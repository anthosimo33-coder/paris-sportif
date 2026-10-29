"""Objets normalisés échangés entre fournisseurs, ingestion et requêtes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

# Marchés canoniques (doivent exister dans market_type / market_selection).
MARKET_SELECTIONS: dict[str, tuple[str, ...]] = {
    "1x2": ("home", "draw", "away"),
    "total_goals": ("over", "under"),
    "asian_handicap": ("home", "away"),
    "btts": ("yes", "no"),
    "draw_no_bet": ("home", "away"),
}
MARKETS_WITH_LINE = frozenset({"total_goals", "asian_handicap"})


@dataclass(frozen=True, slots=True)
class Quote:
    bookmaker_ref: str
    bookmaker_name: str
    market: str
    line: Decimal
    selection: str
    price: Decimal
    source_updated_at: datetime | None = None

    @property
    def key(self) -> tuple[str, str, Decimal, str]:
        return (self.bookmaker_ref, self.market, self.line, self.selection)


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    provider_ref: str
    competition_ref: str
    competition_name: str
    commence_time: datetime
    home_team: str
    away_team: str
    quotes: tuple[Quote, ...]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Une réponse de fournisseur, déjà normalisée, prête à être ingérée."""

    provider: str
    scope: str
    markets: tuple[str, ...]
    observed_at: datetime
    events: tuple[EventSnapshot, ...]
    body: Any
    request_url: str | None = None
    http_status: int | None = None
    unmapped_outcomes: int = 0
    quota_remaining: int | None = None
    quota_used: int | None = None
    quota_last_cost: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
