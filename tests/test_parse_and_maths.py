from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from parisportif import devig
from parisportif.collector import next_interval
from parisportif.normalize import normalize_team_name
from parisportif.providers import the_odds_api
from parisportif.providers.base import ProviderError
from tests.factory import MARKETS, book, event

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
KICKOFF = T0 + timedelta(days=2)


def parse(
    body: list[dict[str, object]], observed_at: datetime = T0, markets: tuple[str, ...] = MARKETS
):
    return the_odds_api.parse_odds_response(
        body, scope="soccer_france_ligue_one", markets=markets, observed_at=observed_at
    )


def test_parse_maps_markets_to_canonical_selections() -> None:
    body = [
        event(
            "e1",
            KICKOFF,
            [
                book(
                    "bk", h2h=(1.5, 4.2, 6.0), totals={2.5: (1.8, 2.05)}, spreads={-0.5: (1.5, 2.6)}
                )
            ],
        )
    ]
    events, unmapped, _ = parse(body)
    assert unmapped == 0
    quotes = {(q.market, q.line, q.selection): q.price for q in events[0].quotes}
    assert quotes[("1x2", Decimal(0), "home")] == Decimal("1.5")
    assert quotes[("1x2", Decimal(0), "draw")] == Decimal("4.2")
    assert quotes[("total_goals", Decimal("2.5"), "under")] == Decimal("2.05")
    # Handicap : les deux côtés rangés sous la ligne du domicile (-0.5), pas +0.5 pour l'extérieur.
    assert quotes[("asian_handicap", Decimal("-0.5"), "home")] == Decimal("1.5")
    assert quotes[("asian_handicap", Decimal("-0.5"), "away")] == Decimal("2.6")
    assert ("asian_handicap", Decimal("0.5"), "away") not in quotes


def test_parse_skips_started_events_and_counts_unknown_outcomes() -> None:
    bad = book("bk", h2h=(1.5, 4.2, 6.0))
    bad["markets"][0]["outcomes"][1]["name"] = "Nul ?"
    bad["markets"][0]["outcomes"][2]["price"] = 1.0
    body = [
        event("started", T0 - timedelta(minutes=1), [book("bk", h2h=(1.5, 4.2, 6.0))]),
        event("e2", KICKOFF, [bad]),
    ]
    events, unmapped, _ = parse(body)
    assert [e.provider_ref for e in events] == ["e2"]
    assert unmapped == 2
    assert len(events[0].quotes) == 1


def test_parse_ignores_markets_not_requested() -> None:
    body = [event("e1", KICKOFF, [book("bk", h2h=(1.5, 4.2, 6.0), totals={2.5: (1.8, 2.05)})])]
    events, _, _ = parse(body, markets=("1x2",))
    assert {q.market for q in events[0].quotes} == {"1x2"}


def test_parse_rejects_non_list_body() -> None:
    with pytest.raises(ProviderError):
        parse({"message": "quota"})


def test_fetch_reads_quota_headers_retries_and_hides_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(the_odds_api.time, "sleep", lambda _s: None)
    calls: list[httpx.Request] = []
    body = [event("e1", datetime.now(UTC) + timedelta(days=1), [book("bk", h2h=(1.5, 4.2, 6.0))])]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(
            200,
            json=body,
            headers={
                "x-requests-remaining": "480",
                "x-requests-used": "20",
                "x-requests-last": "3",
            },
        )

    api = the_odds_api.TheOddsApi(
        "SECRET", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    resp = api.fetch("soccer_epl", datetime.now(UTC))
    assert len(calls) == 2
    assert calls[1].url.params["apiKey"] == "SECRET"
    assert calls[1].url.params["oddsFormat"] == "decimal"
    assert (resp.quota_remaining, resp.quota_used, resp.quota_last_cost) == (480, 20, 3)
    assert resp.request_url is not None and "SECRET" not in resp.request_url
    assert resp.markets == MARKETS


def test_fetch_error_does_not_leak_key() -> None:
    api = the_odds_api.TheOddsApi(
        "SECRET",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, text="invalid api key"))
        ),
    )
    with pytest.raises(ProviderError) as err:
        api.fetch("soccer_epl", datetime.now(UTC))
    assert err.value.http_status == 401
    assert "SECRET" not in str(err.value)


def test_normalize_team_name() -> None:
    assert normalize_team_name("Paris Saint-Germain FC") == "paris saint germain"
    assert normalize_team_name("AS Saint-Étienne") == "saint etienne"
    assert normalize_team_name("Brighton & Hove Albion") == "brighton and hove albion"
    assert normalize_team_name("AS") == "as"
    assert normalize_team_name(normalize_team_name("Olympique de Marseille")) == (
        "olympique de marseille"
    )


def test_devig_methods_sum_to_one_and_power_shrinks_longshot() -> None:
    prices = [1.40, 4.80, 8.50]
    mult = devig.fair_probabilities(prices, "multiplicative")
    powr = devig.fair_probabilities(prices, "power")
    assert sum(mult) == pytest.approx(1)
    assert sum(powr) == pytest.approx(1)
    assert powr[2] < mult[2]  # l'outsider porte plus de marge
    assert powr[0] > mult[0]
    assert devig.fair_probabilities([2.0, 2.0]) == pytest.approx([0.5, 0.5])


def test_ev_and_kelly() -> None:
    assert devig.expected_value(2.10, 1 / 1.95) == pytest.approx(0.0769, abs=1e-4)
    assert devig.kelly_fraction(2.10, 1 / 1.95) == pytest.approx(0.25 * 0.0769 / 1.10, abs=1e-4)
    assert devig.kelly_fraction(1.80, 0.5) == 0.0


def test_cadence_speeds_up_near_kickoff_and_slows_on_low_quota() -> None:
    assert next_interval(30, 10_000, 500) == timedelta(minutes=2)
    assert next_interval(3 * 60, 10_000, 500) == timedelta(minutes=10)
    assert next_interval(5 * 24 * 60, 10_000, 500) == timedelta(hours=3)
    assert next_interval(None, None, 500) == timedelta(hours=6)
    assert next_interval(30, 100, 500) == timedelta(minutes=8)
