"""Cadence de collecte : plus le coup d'envoi approche, plus on interroge souvent.

Partagée par le collecteur (quand repasser) et par la lecture à l'instant T (une cote
confirmée il y a 40 min est fraîche à J-2, périmée à H-1).
"""

from __future__ import annotations

from datetime import timedelta

# (coup d'envoi dans moins de N minutes, intervalle en minutes)
CADENCE: tuple[tuple[int, int], ...] = (
    (60, 2),
    (6 * 60, 10),
    (24 * 60, 30),
    (48 * 60, 60),
)
DEFAULT_INTERVAL = timedelta(hours=3)
IDLE_INTERVAL = timedelta(hours=6)  # aucun match à venir dans l'horizon
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
