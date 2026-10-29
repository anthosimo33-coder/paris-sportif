"""Boucle de collecte : un passage par périmètre, cadence adaptée au prochain coup d'envoi.

Plus le match approche, plus on interroge souvent : c'est près du coup d'envoi (compos,
dernières infos) que les cotes bougent et que la cote de clôture se forme.

Une base perdue arrête le processus (code de sortie non nul) : c'est au superviseur
(systemd, conteneur) de le relancer. Mieux vaut un arrêt visible qu'une boucle qui tourne
sans rien écrire.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from parisportif.cadence import next_interval
from parisportif.ingest import Conn, IngestStats, fail_poll, ingest, start_poll
from parisportif.models import ProviderResponse
from parisportif.providers.base import OddsProvider, ProviderError

log = logging.getLogger("parisportif.collector")

ERROR_INTERVAL = timedelta(minutes=5)
# Un passage encore « running » au-delà de ce délai a été interrompu (crash, coupure).
ABANDONED_AFTER = timedelta(hours=1)


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


def _fail_quietly(conn: Conn, poll_id: int, error: str, http_status: int | None) -> None:
    """Trace l'échec sans masquer l'erreur d'origine si la base est elle-même en cause."""
    try:
        fail_poll(conn, poll_id, error=error, http_status=http_status)
    except Exception:
        log.exception("passage %s : impossible d'enregistrer l'échec", poll_id)


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
        _fail_quietly(conn, poll_id, str(exc), exc.http_status)
        return PollOutcome(None, None, str(exc))
    except Exception as exc:
        _fail_quietly(conn, poll_id, f"fournisseur : {exc.__class__.__name__}", None)
        raise
    try:
        stats = ingest(conn, poll_id, resp, roles=roles)
    except Exception as exc:
        _fail_quietly(conn, poll_id, f"ingestion : {exc}", resp.http_status)
        raise
    for w in resp.warnings:
        log.warning("%s/%s : %s", provider.name, scope, w)
    if stats.new_teams or stats.new_events:
        log.info(
            "%s/%s : %d nouvelles équipes, %d nouveaux matchs (vérifier les doublons)",
            provider.name,
            scope,
            stats.new_teams,
            stats.new_events,
        )
    return PollOutcome(stats, resp, None)


def close_abandoned_polls(conn: Conn, provider: str, now: datetime) -> int:
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE poll_run
            SET status = 'error', error = 'interrompu (processus arrêté)', finished_at = %s
            WHERE provider = %s AND status = 'running' AND started_at < %s
            """,
            (now, provider, now - ABANDONED_AFTER),
        )
    return cur.rowcount


def _db_lost(conn: Conn, exc: BaseException) -> bool:
    return isinstance(exc, psycopg.OperationalError) or conn.closed or conn.broken


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
    closed = close_abandoned_polls(conn, provider.name, clock())
    if closed:
        log.warning("%d passages interrompus marqués en erreur", closed)
    due = dict.fromkeys(scopes, clock())
    polls = 0
    while max_polls is None or polls < max_polls:
        scope = min(due, key=lambda s: due[s])
        wait = (due[scope] - clock()).total_seconds()
        if wait > 0:
            sleep(wait)
        now = clock()
        polls += 1
        try:
            outcome = poll_once(conn, provider, scope, roles, now)
        except Exception as exc:
            if _db_lost(conn, exc):
                log.critical("base de données perdue : arrêt du collecteur")
                raise
            log.exception("%s : échec du passage", scope)
            due[scope] = now + ERROR_INTERVAL
            continue
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
