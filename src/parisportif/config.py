"""Configuration par variables d'environnement (+ fichier des rôles de bookmakers)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROLES = frozenset({"fr_licensed", "sharp_reference", "exchange", "other", "unknown"})


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    odds_api_key: str
    sports: tuple[str, ...]
    regions: tuple[str, ...]
    markets: tuple[str, ...]
    bookmakers: tuple[str, ...]
    horizon_days: int
    quota_reserve: int
    roles_file: Path

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env.get("DATABASE_URL", "postgresql:///parisportif"),
            odds_api_key=env.get("ODDS_API_KEY", ""),
            sports=_csv(env.get("ODDS_API_SPORTS", "soccer_france_ligue_one,soccer_epl")),
            regions=_csv(env.get("ODDS_API_REGIONS", "eu")),
            markets=_csv(env.get("ODDS_API_MARKETS", "h2h,totals,spreads")),
            bookmakers=_csv(env.get("ODDS_API_BOOKMAKERS", "")),
            horizon_days=int(env.get("ODDS_API_HORIZON_DAYS", "7")),
            quota_reserve=int(env.get("ODDS_API_QUOTA_RESERVE", "500")),
            roles_file=Path(env.get("BOOKMAKER_ROLES_FILE", "config/bookmakers.toml")),
        )


def load_roles(path: Path) -> dict[str, str]:
    """{clé bookmaker: rôle}. Fichier absent = aucun rôle (tout en 'unknown')."""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError(f"{path}: section [roles] invalide")
    bad = {k: v for k, v in roles.items() if v not in ROLES}
    if bad:
        raise ValueError(f"{path}: rôles inconnus {bad} (attendus : {sorted(ROLES)})")
    return {str(k): str(v) for k, v in roles.items()}
