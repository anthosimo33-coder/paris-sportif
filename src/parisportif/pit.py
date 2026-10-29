"""Lecture à l'instant T : ce que l'on savait à T, et uniquement ça.

Toutes les fonctions prennent un instant `at`. Le même code sert à l'écran en direct
(at = maintenant) et au backtest (at = n'importe quel instant passé) : pas de fuite
d'information future possible par construction, puisque chaque requête filtre
observed_at <= at.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import TupleRow

from parisportif.cadence import next_interval
from parisportif.devig import Method, expected_value, fair_probabilities, kelly_fraction
from parisportif.models import MARKET_SELECTIONS

Conn = psycopg.Connection[TupleRow]

# Au-delà, l'écart ressemble plus à une erreur de cote (annulable par le book) qu'à de la valeur.
SUSPECT_EDGE = 0.15
# Marge sur la cadence attendue avant de déclarer une source en retard.
CADENCE_SLACK = 1.5


@dataclass(frozen=True, slots=True)
class QuoteState:
    bookmaker: str
    role: str
    market: str
    line: Decimal
    selection: str
    price: Decimal
    observed_at: datetime
    provider: str


@dataclass(frozen=True, slots=True)
class FairLine:
    market: str
    line: Decimal
    probabilities: dict[str, float]
    references: tuple[str, ...]
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValueSignal:
    event_id: int
    kickoff: datetime
    competition: str
    home: str
    away: str
    market: str
    line: Decimal
    selection: str
    bookmaker: str
    price: float
    fair_probability: float
    fair_price: float
    edge: float
    kelly_quarter: float
    references: tuple[str, ...]
    price_seen_at: datetime
    confirmed_at: datetime
    suspect: bool


@dataclass(frozen=True, slots=True)
class ScanResult:
    signals: list[ValueSignal]
    # Écarts au-dessus du seuil écartés parce qu'une source n'a pas confirmé à temps.
    stale_skipped: int


def odds_as_of(conn: Conn, event_id: int, at: datetime) -> list[QuoteState]:
    rows = conn.execute(
        """
        SELECT o.bookmaker, b.role, o.market, o.line, o.selection, o.price, o.observed_at,
               o.provider
        FROM odds_as_of(%s, %s) o
        JOIN bookmaker b ON b.key = o.bookmaker
        ORDER BY o.market, o.line, o.selection, o.bookmaker
        """,
        (event_id, at),
    ).fetchall()
    return [QuoteState(*row) for row in rows]


def kickoff_as_of(conn: Conn, event_id: int, at: datetime) -> datetime | None:
    row = conn.execute("SELECT kickoff_as_of(%s, %s)", (event_id, at)).fetchone()
    return row[0] if row else None


def last_confirmed(
    conn: Conn, event_id: int, market: str, provider: str, at: datetime
) -> datetime | None:
    row = conn.execute(
        "SELECT last_confirmed(%s, %s, %s, %s)", (event_id, market, provider, at)
    ).fetchone()
    return row[0] if row else None


def staleness_allowance(
    kickoff: datetime, confirmed_at: datetime, max_staleness: timedelta
) -> timedelta:
    """Retard toléré : au moins max_staleness, et au moins 1,5 x la cadence attendue.

    Une cote confirmée il y a 40 min est fraîche à J-2 (passage toutes les heures),
    périmée à H-1 (passage toutes les 2 min).
    """
    minutes = (kickoff - confirmed_at).total_seconds() / 60
    return max(max_staleness, CADENCE_SLACK * next_interval(minutes, None, 0))


def fair_lines(
    quotes: list[QuoteState], method: Method = "power"
) -> dict[tuple[str, Decimal], FairLine]:
    """Probabilités justes par (marché, ligne), d'après les books de référence (sharp).

    Une référence ne compte que si elle cote le jeu COMPLET d'issues de la ligne ;
    plusieurs références sont moyennées.
    """
    by_ref: dict[tuple[str, Decimal, str], dict[str, float]] = defaultdict(dict)
    provider_of: dict[str, str] = {}
    for q in quotes:
        if q.role == "sharp_reference":
            by_ref[(q.market, q.line, q.bookmaker)][q.selection] = float(q.price)
            provider_of[q.bookmaker] = q.provider
    acc: dict[tuple[str, Decimal], list[tuple[str, dict[str, float]]]] = defaultdict(list)
    for (market, line, book), prices in by_ref.items():
        selections = MARKET_SELECTIONS[market]
        if set(prices) != set(selections):
            continue
        probs = fair_probabilities([prices[s] for s in selections], method)
        acc[(market, line)].append((book, dict(zip(selections, probs, strict=True))))
    out: dict[tuple[str, Decimal], FairLine] = {}
    for (market, line), refs in acc.items():
        selections = MARKET_SELECTIONS[market]
        mean = {s: sum(r[1][s] for r in refs) / len(refs) for s in selections}
        books = tuple(sorted(r[0] for r in refs))
        providers = tuple(sorted({provider_of[b] for b in books}))
        out[(market, line)] = FairLine(market, line, mean, books, providers)
    return out


class _Confirmations:
    """last_confirmed mis en cache par (source, marché) pour un match et un instant."""

    def __init__(self, conn: Conn, event_id: int, at: datetime) -> None:
        self.conn, self.event_id, self.at = conn, event_id, at
        self._cache: dict[tuple[str, str], datetime | None] = {}

    def oldest(self, providers: set[str], market: str) -> datetime | None:
        """Confirmation la plus ancienne parmi les sources, None si l'une n'a jamais confirmé."""
        values: list[datetime] = []
        for p in providers:
            if (p, market) not in self._cache:
                self._cache[(p, market)] = last_confirmed(
                    self.conn, self.event_id, market, p, self.at
                )
            value = self._cache[(p, market)]
            if value is None:
                return None
            values.append(value)
        return min(values) if values else None


def value_scan(
    conn: Conn,
    at: datetime,
    *,
    min_edge: float = 0.02,
    horizon: timedelta = timedelta(hours=72),
    max_staleness: timedelta = timedelta(minutes=20),
    method: Method = "power",
) -> ScanResult:
    """Écarts entre la meilleure cote FR et la cote juste, tels qu'ils étaient visibles à T.

    Un écart n'est retenu que si la source de la cote FR ET celle de chaque référence ont
    confirmé le match récemment : une source arrêtée ne fabrique pas de faux écarts.
    """
    events = conn.execute(
        """
        SELECT e.event_id, e.commence_time, c.name, h.name, a.name
        FROM events_as_of(%s, %s) e
        JOIN competition c ON c.id = e.competition_id
        JOIN team h ON h.id = e.home_team_id
        JOIN team a ON a.id = e.away_team_id
        ORDER BY e.commence_time, e.event_id
        """,
        (at, horizon),
    ).fetchall()
    signals: list[ValueSignal] = []
    stale: set[tuple[int, str]] = set()
    for event_id, kickoff, comp, home, away in events:
        quotes = odds_as_of(conn, event_id, at)
        fair = fair_lines(quotes, method)
        confirmations = _Confirmations(conn, event_id, at)
        best: dict[tuple[str, Decimal, str], QuoteState] = {}
        for q in quotes:
            if q.role != "fr_licensed" or (q.market, q.line) not in fair:
                continue
            k = (q.market, q.line, q.selection)
            if k not in best or q.price > best[k].price:
                best[k] = q
        for (market, line, selection), q in best.items():
            fl = fair[(market, line)]
            p = fl.probabilities[selection]
            price = float(q.price)
            edge = expected_value(price, p)
            if edge < min_edge:
                continue
            conf = confirmations.oldest({q.provider, *fl.providers}, market)
            if conf is None or at - conf > staleness_allowance(kickoff, conf, max_staleness):
                stale.add((event_id, market))
                continue
            signals.append(
                ValueSignal(
                    event_id=event_id,
                    kickoff=kickoff,
                    competition=comp,
                    home=home,
                    away=away,
                    market=market,
                    line=line,
                    selection=selection,
                    bookmaker=q.bookmaker,
                    price=price,
                    fair_probability=p,
                    fair_price=1.0 / p,
                    edge=edge,
                    kelly_quarter=kelly_fraction(price, p),
                    references=fl.references,
                    price_seen_at=q.observed_at,
                    confirmed_at=conf,
                    suspect=edge > SUSPECT_EDGE,
                )
            )
    signals.sort(key=lambda s: s.edge, reverse=True)
    return ScanResult(signals, len(stale))


@dataclass(frozen=True, slots=True)
class Closing:
    kickoff: datetime
    at: datetime
    confirmed_at: datetime | None
    fair: FairLine | None


def closing_fair(
    conn: Conn, event_id: int, market: str, line: Decimal, method: Method = "power"
) -> Closing | None:
    """Ligne juste à la clôture : état juste avant le coup d'envoi (le plus tôt annoncé)."""
    row = conn.execute("SELECT kickoff_as_of(%s, 'infinity')", (event_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    kickoff: datetime = row[0]
    at = kickoff - timedelta(microseconds=1)
    fair = fair_lines(odds_as_of(conn, event_id, at), method).get((market, line))
    confirmed = (
        _Confirmations(conn, event_id, at).oldest(set(fair.providers), market) if fair else None
    )
    return Closing(kickoff, at, confirmed, fair)


def clv(taken_price: float, closing_probability: float) -> float:
    """Closing line value : +0.03 = cote prise 3 % au-dessus de la cote juste de clôture."""
    return expected_value(taken_price, closing_probability)
