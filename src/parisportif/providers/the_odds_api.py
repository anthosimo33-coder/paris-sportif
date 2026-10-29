"""Fournisseur The Odds API (v4).

Format de réponse attendu de GET /v4/sports/{sport}/odds :
    [{"id", "sport_key", "sport_title", "commence_time", "home_team", "away_team",
      "bookmakers": [{"key", "title", "last_update",
                      "markets": [{"key", "last_update",
                                   "outcomes": [{"name", "price", "point"?}]}]}]}]

Coût annoncé par la source : 1 crédit par marché et par région à chaque appel.
Clés de bookmakers et régions réellement disponibles : à vérifier avec une vraie clé
(`parisportif bookmakers` après la première collecte).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from parisportif.models import EventSnapshot, ProviderResponse, Quote
from parisportif.providers.base import ProviderError

NAME = "the_odds_api"
BASE_URL = "https://api.the-odds-api.com/v4"

# Clé de marché de la source -> marché canonique.
MARKET_MAP: dict[str, str] = {
    "h2h": "1x2",
    "totals": "total_goals",
    "spreads": "asian_handicap",
    "btts": "btts",
    "draw_no_bet": "draw_no_bet",
}

# Marchés servis par l'appel groupé /sports/{sport}/odds. Les autres (btts, draw_no_bet…)
# exigent l'appel par match /events/{id}/odds, plus coûteux : pas encore implémenté.
BULK_MARKETS = frozenset({"h2h", "totals", "spreads"})

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:  # la source parle UTC ; une date naïve est interprétée comme telle
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _selection(market: str, name: str, home: str, away: str) -> str | None:
    n = name.strip()
    low = n.lower()
    if market in ("1x2", "draw_no_bet", "asian_handicap"):
        if n == home:
            return "home"
        if n == away:
            return "away"
        if market == "1x2" and low == "draw":
            return "draw"
        return None
    if market == "total_goals":
        return low if low in ("over", "under") else None
    if market == "btts":
        return low if low in ("yes", "no") else None
    return None


def _line(market: str, selection: str, point: Any) -> Decimal | None:
    """Ligne canonique. Handicap asiatique : toujours du point de vue du domicile."""
    if market not in ("total_goals", "asian_handicap"):
        return Decimal(0)
    p = _decimal(point)
    if p is None:
        return None
    if market == "asian_handicap" and selection == "away":
        p = -p
    return p


def parse_odds_response(
    body: Any, *, scope: str, markets: Iterable[str], observed_at: datetime
) -> tuple[tuple[EventSnapshot, ...], int, tuple[str, ...]]:
    """Réponse brute -> (événements normalisés, issues non reconnues, avertissements).

    Ignore les événements dont le coup d'envoi est passé à observed_at : le produit
    travaille en avant-match, et la cote de clôture est la dernière vue AVANT le coup d'envoi.
    """
    if not isinstance(body, list):
        raise ProviderError(f"réponse inattendue : liste attendue, reçu {type(body).__name__}")
    wanted = set(markets)
    unmapped = 0
    warnings: list[str] = []
    events: list[EventSnapshot] = []
    for raw in body:
        if not isinstance(raw, Mapping):
            unmapped += 1
            continue
        commence = _parse_dt(raw.get("commence_time"))
        home, away = raw.get("home_team"), raw.get("away_team")
        event_id = raw.get("id")
        if not (commence and isinstance(home, str) and isinstance(away, str) and event_id):
            warnings.append(f"événement ignoré (champs manquants) : {event_id!r}")
            continue
        home, away = home.strip(), away.strip()
        if home == away:
            warnings.append(f"événement ignoré (domicile = extérieur) : {event_id!r}")
            continue
        if commence <= observed_at:
            continue
        quotes: dict[tuple[str, str, Decimal, str], Quote] = {}
        for bk in raw.get("bookmakers") or []:
            if not isinstance(bk, Mapping) or not isinstance(bk.get("key"), str):
                unmapped += 1
                continue
            bk_key = bk["key"]
            title = bk.get("title")
            bk_title = title if isinstance(title, str) else bk_key
            for mk in bk.get("markets") or []:
                if not isinstance(mk, Mapping):
                    unmapped += 1
                    continue
                market = MARKET_MAP.get(str(mk.get("key")))
                if market is None or market not in wanted:
                    continue
                updated = _parse_dt(mk.get("last_update")) or _parse_dt(bk.get("last_update"))
                for oc in mk.get("outcomes") or []:
                    if not isinstance(oc, Mapping) or not isinstance(oc.get("name"), str):
                        unmapped += 1
                        continue
                    sel = _selection(market, oc["name"], home, away)
                    price = _decimal(oc.get("price"))
                    if sel is None or price is None or price <= 1:
                        unmapped += 1
                        continue
                    line = _line(market, sel, oc.get("point"))
                    if line is None:
                        unmapped += 1
                        continue
                    q = Quote(bk_key, bk_title, market, line, sel, price, updated)
                    if q.key in quotes:
                        warnings.append(f"doublon {q.key} sur {event_id!r}, dernière valeur gardée")
                    quotes[q.key] = q
        events.append(
            EventSnapshot(
                provider_ref=str(event_id),
                competition_ref=str(raw.get("sport_key") or scope),
                competition_name=str(raw.get("sport_title") or raw.get("sport_key") or scope),
                commence_time=commence,
                home_team=home,
                away_team=away,
                quotes=tuple(quotes.values()),
            )
        )
    return tuple(events), unmapped, tuple(warnings)


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


class TheOddsApi:
    name = NAME

    def __init__(
        self,
        api_key: str,
        *,
        regions: Iterable[str] = ("eu",),
        markets: Iterable[str] = ("h2h", "totals", "spreads"),
        bookmakers: Iterable[str] = (),
        horizon: timedelta = timedelta(days=7),
        client: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY manquant")
        unknown = [m for m in markets if m not in BULK_MARKETS]
        if unknown:
            raise ValueError(
                f"marchés non servis par /sports/{{sport}}/odds : {unknown} "
                f"(possibles : {sorted(BULK_MARKETS)} ; les autres passent par l'appel par match)"
            )
        self._api_key = api_key
        self._regions = tuple(regions)
        self._source_markets = tuple(markets)
        self._bookmakers = tuple(bookmakers)
        self._horizon = horizon
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0))
        self._max_retries = max_retries

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(MARKET_MAP[m] for m in self._source_markets))

    def _params(self, now: datetime) -> dict[str, str]:
        params = {
            "markets": ",".join(self._source_markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeFrom": _iso_z(now),
            "commenceTimeTo": _iso_z(now + self._horizon),
        }
        # Côté source, `bookmakers` prime sur `regions` quand les deux sont fournis.
        if self._bookmakers:
            params["bookmakers"] = ",".join(self._bookmakers)
        else:
            params["regions"] = ",".join(self._regions)
        return params

    def request_url(self, scope: str, now: datetime) -> str:
        return f"{BASE_URL}/sports/{scope}/odds?{urlencode(self._params(now))}"

    def fetch(self, scope: str, now: datetime) -> ProviderResponse:
        url = f"{BASE_URL}/sports/{scope}/odds"
        params = {**self._params(now), "apiKey": self._api_key}
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise ProviderError(f"réseau : {exc.__class__.__name__}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                time.sleep(2**attempt)
                continue
            break
        assert response is not None
        observed_at = datetime.now(UTC)
        if response.status_code != 200:
            # Ne jamais renvoyer l'URL complète : elle contient la clé.
            raise ProviderError(
                f"HTTP {response.status_code} : {response.text[:300]}",
                http_status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("réponse non JSON", http_status=response.status_code) from exc
        events, unmapped, warnings = parse_odds_response(
            body, scope=scope, markets=self.markets, observed_at=observed_at
        )
        return ProviderResponse(
            provider=NAME,
            scope=scope,
            markets=self.markets,
            observed_at=observed_at,
            events=events,
            body=body,
            request_url=self.request_url(scope, now),
            http_status=response.status_code,
            unmapped_outcomes=unmapped,
            quota_remaining=_int_header(response.headers, "x-requests-remaining"),
            quota_used=_int_header(response.headers, "x-requests-used"),
            quota_last_cost=_int_header(response.headers, "x-requests-last"),
            warnings=warnings,
        )
