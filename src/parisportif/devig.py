"""Retrait de la marge : cotes d'un bookmaker -> probabilités « justes ».

Deux méthodes :
- multiplicative : on normalise les probabilités implicites. Simple, mais retire la marge
  à parts égales alors que les books margent surtout les outsiders (biais favori-outsider).
- power (par défaut) : p_i = (1/o_i)^k, k tel que la somme vaille 1. Retire davantage
  aux outsiders, plus proche de la réalité sur le 1X2 foot.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

Method = Literal["multiplicative", "power"]


def overround(prices: Sequence[float]) -> float:
    """Somme des probabilités implicites. 1.05 = 5 % de marge au sens de l'overround."""
    if not prices or any(p <= 1 for p in prices):
        raise ValueError("cotes décimales > 1 attendues")
    return sum(1.0 / p for p in prices)


def fair_probabilities(prices: Sequence[float], method: Method = "power") -> list[float]:
    """Probabilités sans marge, dans l'ordre des cotes fournies (jeu complet d'issues)."""
    if len(prices) < 2:
        raise ValueError("au moins deux issues")
    implied = [1.0 / p for p in prices]
    total = overround(prices)
    if method == "multiplicative" or abs(total - 1.0) < 1e-12:
        return [x / total for x in implied]
    if method != "power":
        raise ValueError(f"méthode inconnue : {method}")
    # sum(x_i^k) est décroissante en k pour x_i dans ]0,1[ : bissection sur k.
    lo, hi = (1.0, 10.0) if total > 1 else (0.01, 1.0)
    for _ in range(200):
        mid = (lo + hi) / 2
        s = sum(x**mid for x in implied)
        if abs(s - 1.0) < 1e-13:
            break
        if s > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    probs = [x**k for x in implied]
    norm = sum(probs)
    return [p / norm for p in probs]


def expected_value(price: float, probability: float) -> float:
    """EV par unité misée : +0.05 = on gagne 5 centimes par euro en moyenne."""
    return price * probability - 1.0


def kelly_fraction(price: float, probability: float, fraction: float = 0.25) -> float:
    """Part de bankroll à miser (Kelly fractionné, 1/4 par défaut). 0 si EV <= 0."""
    edge = expected_value(price, probability)
    if edge <= 0:
        return 0.0
    return fraction * edge / (price - 1.0)
