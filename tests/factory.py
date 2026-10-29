"""Fabrique de réponses au format The Odds API v4 (données fictives)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from parisportif.models import ProviderResponse
from parisportif.providers.the_odds_api import NAME, parse_odds_response

MARKETS = ("1x2", "total_goals", "asian_handicap")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def book(
    key: str,
    *,
    h2h: tuple[float, float, float] | None = None,
    totals: dict[float, tuple[float, float]] | None = None,
    spreads: dict[float, tuple[float, float]] | None = None,
    home: str = "Paris Saint Germain",
    away: str = "RC Lens",
    title: str | None = None,
) -> dict[str, Any]:
    """spreads : {handicap domicile: (cote domicile, cote extérieur)}."""
    markets: list[dict[str, Any]] = []
    if h2h is not None:
        markets.append(
            {
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": h2h[0]},
                    {"name": "Draw", "price": h2h[1]},
                    {"name": away, "price": h2h[2]},
                ],
            }
        )
    if totals:
        markets.append(
            {
                "key": "totals",
                "outcomes": [
                    o
                    for line, (over, under) in totals.items()
                    for o in (
                        {"name": "Over", "price": over, "point": line},
                        {"name": "Under", "price": under, "point": line},
                    )
                ],
            }
        )
    if spreads:
        markets.append(
            {
                "key": "spreads",
                "outcomes": [
                    o
                    for line, (h, a) in spreads.items()
                    for o in (
                        {"name": home, "price": h, "point": line},
                        {"name": away, "price": a, "point": -line},
                    )
                ],
            }
        )
    return {
        "key": key,
        "title": title or key,
        "last_update": "2026-10-30T10:00:00Z",
        "markets": markets,
    }


def event(
    event_id: str,
    commence: datetime,
    bookmakers: list[dict[str, Any]],
    *,
    home: str = "Paris Saint Germain",
    away: str = "RC Lens",
    sport: str = "soccer_france_ligue_one",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "sport_key": sport,
        "sport_title": "Ligue 1 - France",
        "commence_time": iso(commence),
        "home_team": home,
        "away_team": away,
        "bookmakers": bookmakers,
    }


def response(
    body: list[dict[str, Any]],
    observed_at: datetime,
    scope: str = "soccer_france_ligue_one",
    markets: tuple[str, ...] = MARKETS,
) -> ProviderResponse:
    events, unmapped, warnings = parse_odds_response(
        body, scope=scope, markets=markets, observed_at=observed_at
    )
    return ProviderResponse(
        provider=NAME,
        scope=scope,
        markets=markets,
        observed_at=observed_at,
        events=events,
        body=body,
        unmapped_outcomes=unmapped,
        warnings=warnings,
    )
