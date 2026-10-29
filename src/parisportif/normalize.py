"""Normalisation des noms d'équipes pour la résolution d'entités entre sources."""

from __future__ import annotations

import re
import unicodedata


def normalize_team_name(name: str) -> str:
    """« Paris Saint-Germain » -> « paris saint germain ». Déterministe et idempotent.

    Volontairement prudente : casse, accents et ponctuation seulement. Les sigles (FC, SC,
    AS…) sont GARDÉS : « Barcelona » et « Barcelona SC » sont deux clubs. Mieux vaut un
    doublon visible, rattaché ensuite par un alias manuel, qu'une fusion silencieuse.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())
