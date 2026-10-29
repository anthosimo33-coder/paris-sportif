"""Normalisation des noms d'équipes pour la résolution d'entités entre sources."""

from __future__ import annotations

import re
import unicodedata

# Suffixes/préfixes juridiques sans valeur d'identification. Volontairement court :
# « Stade », « Olympique », « Racing » distinguent des clubs et ne sont PAS retirés.
_LEGAL_TOKENS = frozenset({"fc", "afc", "cf", "sc", "ssc", "ac", "as", "sv", "fk", "cd", "club"})


def normalize_team_name(name: str) -> str:
    """« Paris Saint-Germain FC » -> « paris saint germain ». Déterministe et idempotent."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t not in _LEGAL_TOKENS]
    if not tokens:  # un nom fait uniquement de sigles (« AS ») reste tel quel
        tokens = text.split()
    return " ".join(tokens)
