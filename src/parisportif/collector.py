"""Boucle de collecte : un passage par périmètre, cadence adaptée au prochain coup d'envoi.

Plus le match approche, plus on interroge souvent : c'est près du coup d'envoi (compos,
dernières infos) que les cotes bougent et que la cote de clôture se forme.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parisportif.ingest import Conn, IngestStats, fail_poll, ingest, start_poll
from parisportif.models import ProviderResponse
from parisportif.providers.base import OddsProvider, ProviderError

log = logging.getLogger("parisportif.collector")

# (coup d'envoi dans moins de N minutes, intervalle en minutes)
CADENCE: tuple[tuple[int, int], ...] = (
    (60, 2),
    (6 * 60, 10),
    (24 * 60, 30),
    (48 * 60, 60),
)
DEFAULT_INTERVAL = timedelta(hours=3)
IDLE_INTERVAL = timedelta(hours=6)  # aucun match à venir dans l'horizon
ERROR_INTERVAL = timedelta(minutes=5)
LOW_QUOTA_FACTOR = 4


def next_interval(
    minutes_to_kickoff: float | None, quota_remaining: int | None, quota_reserve: int
) -> timedelta:
    if minutes_to_kickoff is None:
        interval = IDLE_INTERVAL
    else:
        interval = DEFAULT_INTERVAL
        for threshold, minutes in CADENCE:
            if minutes_to_kickoff < threshold:
                interval = timedelta(minutes=minutes)
                break
    if quota_remaining is not None and quota_remaining < quota_reserve:
        interval *= LOW_QUOTA_FACTOR
    return interval


def minutes_to_next_kickoff(resp: ProviderResponse) -> float | None:
    upcoming = [e.commence_time for e in resp.events if e.commence_time > resp.observed_at]
    if not upcoming:
        return None
    return (min(upcoming) - resp.observed_at).total_seconds() / 60


@dataclass(frozen=True, slots=True)
class PollOutcome:
    stats: IngestStats | None
    response: ProviderResponse | None
    error: str | None


def poll_once(
    conn: Conn, provider: OddsProvider, scope: str, roles: Mapping[str, str], now: datetime
) -> PollOutcome:
    poll_id = start_poll(
        conn,
        provider=provider.name,
        scope=scope,
        markets=provider.markets,
        request_url=provider.request_url(scope, now),
    )
    try:
        resp = provider.fetch(scope, now)
    except ProviderError as exc:
        fail_poll(conn, poll_id, error=str(exc), http_status=exc.http_status)
        return PollOutcome(None, None, str(exc))
    try:
        stats = ingest(conn, poll_id, resp, roles=roles)
    except Exception as exc:
        fail_poll(conn, poll_id, error=f"ingestion : {exc}", http_status=resp.http_status)
        raise
    for w in resp.warnings:
        log.warning("%s/%s : %s", provider.name, scope, w)
    return PollOutcome(stats, resp, None)


def run_forever(
    conn: Conn,
    provider: OddsProvider,
    scopes: tuple[str, ...],
    roles: Mapping[str, str],
    *,
    quota_reserve: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> None:
    due = dict.fromkeys(scopes, clock())
    polls = 0
    while max_polls is None or polls < max_polls:
        scope = min(due, key=lambda s: due[s])
        wait = (due[scope] - clock()).total_seconds()
        if wait > 0:
            sleep(wait)
        now = clock()
        try:
            outcome = poll_once(conn, provider, scope, roles, now)
        except Exception:
            log.exception("%s : échec d'ingestion", scope)
            due[scope] = now + ERROR_INTERVAL
            polls += 1
            continue
        polls += 1
        if outcome.response is None or outcome.stats is None:
            log.error("%s : %s", scope, outcome.error)
            due[scope] = now + ERROR_INTERVAL
            continue
        resp, stats = outcome.response, outcome.stats
        interval = next_interval(minutes_to_next_kickoff(resp), resp.quota_remaining, quota_reserve)
        due[scope] = now + interval
        log.info(
            "%s : %d matchs, +%d cotes, -%d retraits, quota restant %s, prochain dans %s",
            scope,
            stats.events_seen,
            stats.quotes_inserted,
            stats.quotes_removed,
            resp.quota_remaining,
            interval,
        )
