"""Génère une journée fictive (dimanche 1er novembre 2026) au format The Odds API v4.

Quatre passages du collecteur, enveloppés avec leur instant d'observation :
  14:00Z  ouverture        : quelques écarts, dont un « trop beau » (Lens à 9,50)
  15:00Z  Winamax décroche : Plus de 2,5 PSG-Lens à 2,10 (juste ~1,95)
  15:50Z  Winamax corrige  : l'écart disparaît
  19:40Z  clôture PSG-Lens : sert à mesurer la CLV du pari pris à 15:00Z

Toutes les cotes sont INVENTÉES. Usage : python examples/demo/make_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent
SCOPES = {"L1": ("soccer_france_ligue_one", "Ligue 1 - France"), "PL": ("soccer_epl", "EPL")}

MATCHES = {
    "psg-lens": ("L1", "Paris Saint Germain", "RC Lens", "2026-11-01T19:45:00Z"),
    "om-ol": ("L1", "Marseille", "Lyon", "2026-11-01T16:15:00Z"),
    "ars-che": ("PL", "Arsenal", "Chelsea", "2026-11-01T16:30:00Z"),
}

# cotes[passage][match][book] = {"h2h": (1, N, 2), "totals": {ligne: (plus, moins)},
#                                "spreads": {handicap domicile: (dom, ext)}}
Book = dict[str, Any]
BASE: dict[str, dict[str, Book]] = {
    "psg-lens": {
        "pinnacle": {"h2h": (1.42, 4.95, 7.40), "totals": {2.5: (1.88, 2.00)}},
        "winamax_fr": {"h2h": (1.38, 4.60, 6.60), "totals": {2.5: (1.80, 1.92)}},
        "betclic_fr": {"h2h": (1.37, 4.50, 6.80), "totals": {2.5: (1.82, 1.90)}},
        "unibet_fr": {"h2h": (1.39, 4.55, 6.50), "totals": {2.5: (1.79, 1.93)}},
    },
    "om-ol": {
        "pinnacle": {"h2h": (2.05, 3.60, 3.55), "totals": {2.5: (1.80, 2.08)}},
        "winamax_fr": {"h2h": (1.98, 3.40, 3.35), "totals": {2.5: (1.74, 1.98)}},
        "betclic_fr": {"h2h": (1.97, 3.45, 3.30)},
        "unibet_fr": {"h2h": (1.92, 3.35, 3.95), "totals": {2.5: (1.75, 1.97)}},
    },
    "ars-che": {
        "pinnacle": {"h2h": (1.85, 3.85, 4.35), "spreads": {-0.5: (1.87, 2.05)}},
        "winamax_fr": {"h2h": (1.80, 3.60, 4.10), "spreads": {-0.5: (1.80, 1.98)}},
        "betclic_fr": {"h2h": (1.78, 3.65, 4.20), "spreads": {-0.5: (1.79, 2.18)}},
        "unibet_fr": {"h2h": (1.80, 3.55, 4.15)},
    },
}


def _with(match: str, book: str, **changes: Any) -> dict[str, dict[str, Book]]:
    data = json.loads(json.dumps(BASE, default=list))  # copie profonde
    for k, v in changes.items():
        data[match][book][k] = v
    return data


POLLS: dict[str, tuple[str, dict[str, dict[str, Book]]]] = {
    # Unibet affiche Lens à 9,50 (juste ~8) : trop beau, probablement une erreur de cote.
    "1_ouverture": ("2026-11-01T14:00:00Z", _with("psg-lens", "unibet_fr", h2h=(1.39, 4.55, 9.50))),
    "2_ecart_winamax": (
        "2026-11-01T15:00:00Z",
        _with("psg-lens", "winamax_fr", totals={2.5: (2.10, 1.70)}),
    ),
    "3_correction": ("2026-11-01T15:50:00Z", BASE),
    "4_cloture_psg": (
        "2026-11-01T19:40:00Z",
        _with("psg-lens", "pinnacle", h2h=(1.40, 5.10, 7.80), totals={2.5: (1.95, 1.93)}),
    ),
}

TITLES = {
    "pinnacle": "Pinnacle",
    "winamax_fr": "Winamax",
    "betclic_fr": "Betclic",
    "unibet_fr": "Unibet",
}


def _markets(home: str, away: str, odds: Book) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "h2h" in odds:
        h, d, a = odds["h2h"]
        out.append(
            {
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": h},
                    {"name": "Draw", "price": d},
                    {"name": away, "price": a},
                ],
            }
        )
    if "totals" in odds:
        out.append(
            {
                "key": "totals",
                "outcomes": [
                    o
                    for line, (over, under) in odds["totals"].items()
                    for o in (
                        {"name": "Over", "price": over, "point": float(line)},
                        {"name": "Under", "price": under, "point": float(line)},
                    )
                ],
            }
        )
    if "spreads" in odds:
        out.append(
            {
                "key": "spreads",
                "outcomes": [
                    o
                    for line, (hp, ap) in odds["spreads"].items()
                    for o in (
                        {"name": home, "price": hp, "point": float(line)},
                        {"name": away, "price": ap, "point": -float(line)},
                    )
                ],
            }
        )
    return out


def main() -> None:
    for name, (observed_at, odds) in POLLS.items():
        for code, (scope, title) in SCOPES.items():
            events = []
            for match_id, (league, home, away, kickoff) in MATCHES.items():
                if league != code or kickoff <= observed_at:
                    continue
                events.append(
                    {
                        "id": f"demo-{match_id}",
                        "sport_key": scope,
                        "sport_title": title,
                        "commence_time": kickoff,
                        "home_team": home,
                        "away_team": away,
                        "bookmakers": [
                            {
                                "key": bk,
                                "title": TITLES[bk],
                                "last_update": observed_at,
                                "markets": _markets(home, away, o),
                            }
                            for bk, o in odds[match_id].items()
                        ],
                    }
                )
            path = OUT / f"{name}_{code}.json"
            path.write_text(
                json.dumps(
                    {
                        "scope": scope,
                        "observed_at": observed_at,
                        "markets": ["h2h", "totals", "spreads"],
                        "response": events,
                    },
                    indent=1,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            print(path.name, len(events), "match(s)")


if __name__ == "__main__":
    main()
