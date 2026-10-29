"""Garanties du journal à l'instant T, testées sur un vrai Postgres."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from parisportif import devig, pit
from parisportif.collector import poll_once
from parisportif.ingest import OutOfOrderError, ingest, start_poll
from parisportif.models import ProviderResponse
from parisportif.providers.base import ProviderError
from tests.factory import MARKETS, book, event, response

Conn = psycopg.Connection[TupleRow]

KICKOFF = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=30)
T3 = T1 + timedelta(minutes=60)
T4 = T1 + timedelta(minutes=90)
ROLES = {"pinnacle": "sharp_reference", "winamax_fr": "fr_licensed", "betclic_fr": "fr_licensed"}

PINNACLE = book("pinnacle", h2h=(1.50, 4.40, 6.50), totals={2.5: (1.85, 2.02)})
BETCLIC = book("betclic_fr", h2h=(1.47, 4.20, 6.00))


def winamax(over: float) -> dict[str, Any]:
    return book("winamax_fr", h2h=(1.45, 4.30, 6.20), totals={2.5: (over, 1.95)})


def poll(
    conn: Conn,
    books: list[dict[str, Any]],
    at: datetime,
    *,
    kickoff: datetime = KICKOFF,
    event_id: str = "e1",
) -> int:
    resp = response([event(event_id, kickoff, books)], at)
    return ingest_resp(conn, resp)


def ingest_resp(conn: Conn, resp: ProviderResponse) -> int:
    poll_id = start_poll(
        conn, provider=resp.provider, scope=resp.scope, markets=resp.markets, request_url=None
    )
    stats = ingest(conn, poll_id, resp, roles=ROLES)
    return stats.quotes_inserted + stats.quotes_removed


def count(conn: Conn, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def price_at(
    conn: Conn, at: datetime, bookmaker: str, market: str, selection: str, line: str = "0"
) -> Decimal | None:
    for q in pit.odds_as_of(conn, 1, at):
        if (q.bookmaker, q.market, q.selection, q.line) == (
            bookmaker,
            market,
            selection,
            Decimal(line),
        ):
            return q.price
    return None


@pytest.fixture
def timeline(conn: Conn) -> Conn:
    """T1 : 3 books. T2 : Winamax monte le Plus de 2,5 à 2,10, Betclic disparaît.
    T3 : Betclic revient avec les mêmes cotes."""
    assert poll(conn, [PINNACLE, winamax(1.80), BETCLIC], T1) == 13
    assert poll(conn, [PINNACLE, winamax(2.10)], T2) == 1 + 3
    assert poll(conn, [PINNACLE, winamax(2.10), BETCLIC], T3) == 3
    return conn


def test_journal_stores_only_changes(timeline: Conn) -> None:
    assert count(timeline, "SELECT count(*) FROM odds_quote") == 13 + 4 + 3
    assert count(timeline, "SELECT count(*) FROM odds_quote WHERE price IS NULL") == 3
    # Un passage identique n'écrit aucune cote, mais confirme l'événement (fraîcheur).
    assert poll(timeline, [PINNACLE, winamax(2.10), BETCLIC], T4) == 0
    assert count(timeline, "SELECT count(*) FROM poll_event") == 4


def test_as_of_returns_what_was_known_at_t(timeline: Conn) -> None:
    assert pit.odds_as_of(timeline, 1, T1 - timedelta(seconds=1)) == []
    assert price_at(
        timeline, T1 + timedelta(minutes=5), "winamax_fr", "total_goals", "over", "2.5"
    ) == Decimal("1.80")
    assert price_at(timeline, T2, "winamax_fr", "total_goals", "over", "2.5") == Decimal("2.10")
    # Retirée à T2, de retour à T3.
    assert price_at(timeline, T2 - timedelta(seconds=1), "betclic_fr", "1x2", "home") == Decimal(
        "1.47"
    )
    assert price_at(timeline, T2, "betclic_fr", "1x2", "home") is None
    assert price_at(timeline, T3, "betclic_fr", "1x2", "home") == Decimal("1.47")


def test_projection_equals_rebuild_from_journal(timeline: Conn) -> None:
    snapshot = "SELECT * FROM odds_current ORDER BY 1, 2, 3, 4, 5"
    before = timeline.execute(snapshot).fetchall()
    timeline.execute("SELECT rebuild_odds_current()")
    assert timeline.execute(snapshot).fetchall() == before


def test_history_is_append_only(timeline: Conn) -> None:
    for sql in (
        "UPDATE odds_quote SET price = 9",
        "DELETE FROM odds_quote",
        "TRUNCATE odds_quote CASCADE",
        "DELETE FROM event_schedule",
        "UPDATE poll_event SET observed_at = now()",
    ):
        with pytest.raises(psycopg.errors.RaiseException), timeline.transaction():
            timeline.execute(sql)


def test_out_of_order_observation_is_rejected_atomically(timeline: Conn) -> None:
    before = count(timeline, "SELECT count(*) FROM odds_quote")
    with pytest.raises(OutOfOrderError):
        poll(timeline, [PINNACLE, winamax(3.00)], T2)
    assert count(timeline, "SELECT count(*) FROM odds_quote") == before
    assert count(timeline, "SELECT count(*) FROM poll_run WHERE status = 'running'") == 1


def test_kickoff_change_is_versioned(timeline: Conn) -> None:
    later = KICKOFF + timedelta(hours=1)
    poll(timeline, [PINNACLE, winamax(2.10), BETCLIC], T4, kickoff=later)
    assert pit.kickoff_as_of(timeline, 1, T3) == KICKOFF
    assert pit.kickoff_as_of(timeline, 1, T4) == later
    # Un événement n'existe pas avant sa première observation.
    assert (
        timeline.execute(
            "SELECT * FROM events_as_of(%s, '7 days')", (T1 - timedelta(seconds=1),)
        ).fetchall()
        == []
    )


def test_value_scan_finds_the_gap_only_once_it_exists(timeline: Conn) -> None:
    assert pit.value_scan(timeline, T1 + timedelta(minutes=5)) == []
    signals = pit.value_scan(timeline, T2 + timedelta(minutes=1))
    assert len(signals) == 1
    s = signals[0]
    p_over = devig.fair_probabilities([1.85, 2.02])[0]
    assert (s.bookmaker, s.market, s.selection, s.line) == (
        "winamax_fr",
        "total_goals",
        "over",
        Decimal("2.5"),
    )
    assert s.edge == pytest.approx(2.10 * p_over - 1)
    assert s.references == ("pinnacle",)
    assert s.confirmed_at == T2
    assert not s.suspect


def test_value_scan_refuses_stale_data(timeline: Conn) -> None:
    late = T3 + timedelta(minutes=25)  # plus aucun passage depuis T3
    assert pit.value_scan(timeline, late, max_staleness=timedelta(minutes=20)) == []
    assert len(pit.value_scan(timeline, late, max_staleness=timedelta(minutes=30))) == 1


def test_closing_line_is_last_state_before_kickoff(timeline: Conn) -> None:
    poll(
        timeline,
        [
            book("pinnacle", h2h=(1.50, 4.40, 6.50), totals={2.5: (1.95, 1.92)}),
            winamax(2.10),
            BETCLIC,
        ],
        KICKOFF - timedelta(minutes=2),
    )
    close = pit.closing_fair(timeline, 1, "total_goals", Decimal("2.5"))
    assert close is not None and close.fair is not None
    assert close.confirmed_at == KICKOFF - timedelta(minutes=2)
    p_close = devig.fair_probabilities([1.95, 1.92])[0]
    assert close.fair.probabilities["over"] == pytest.approx(p_close)
    # Pris à 2,10 à T2 : la valeur se mesure contre la clôture, pas contre le résultat.
    assert pit.clv(2.10, p_close) == pytest.approx(2.10 * p_close - 1)


def _other_source(at: datetime, away: str) -> ProviderResponse:
    body = [
        event(
            "ref-autre-source",
            KICKOFF,
            [book("zebet_fr", h2h=(1.48, 4.25, 6.10), home="Paris Saint-Germain FC", away=away)],
            home="Paris Saint-Germain FC",
            away=away,
        )
    ]
    return replace(response(body, at), provider="autre_source")


def test_unknown_team_name_creates_a_separate_event(timeline: Conn) -> None:
    # « Lens » != « RC Lens » une fois normalisé : sans alias, c'est une autre équipe.
    ingest_resp(timeline, _other_source(T4, "Lens"))
    assert count(timeline, "SELECT count(*) FROM event") == 2


def test_second_source_maps_to_same_event_and_does_not_remove_others(timeline: Conn) -> None:
    timeline.execute(
        "INSERT INTO team_alias (alias_norm, team_id, origin) VALUES ('lens', 2, 'manual')"
    )
    ingest_resp(timeline, _other_source(T4, "Lens"))
    assert count(timeline, "SELECT count(*) FROM event") == 1
    assert count(timeline, "SELECT count(*) FROM event_source WHERE event_id = 1") == 2
    assert price_at(timeline, T4, "zebet_fr", "1x2", "home") == Decimal("1.48")
    # Cette source ne cote pas Pinnacle : les cotes Pinnacle (autre source) restent en place.
    assert price_at(timeline, T4, "pinnacle", "1x2", "home") == Decimal("1.50")


class _FailingProvider:
    name = "the_odds_api"
    markets = MARKETS

    def request_url(self, scope: str, now: datetime) -> str:
        return f"https://example.invalid/{scope}"

    def fetch(self, scope: str, now: datetime) -> ProviderResponse:
        raise ProviderError("HTTP 429 : quota", http_status=429)


def test_failed_poll_is_traced(conn: Conn) -> None:
    out = poll_once(conn, _FailingProvider(), "soccer_epl", ROLES, T1)
    assert out.stats is None and out.error is not None
    row = conn.execute("SELECT status, http_status, error FROM poll_run").fetchone()
    assert row == ("error", 429, "HTTP 429 : quota")
