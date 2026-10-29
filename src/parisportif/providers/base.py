from __future__ import annotations

from datetime import datetime
from typing import Protocol

from parisportif.models import ProviderResponse


class ProviderError(Exception):
    """Échec d'appel côté fournisseur (HTTP, réseau, format)."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class OddsProvider(Protocol):
    name: str

    @property
    def markets(self) -> tuple[str, ...]:
        """Marchés canoniques couverts par chaque appel (sert à la détection des retraits)."""
        ...

    def request_url(self, scope: str, now: datetime) -> str:
        """URL appelée, sans secret (journalisée dans poll_run)."""
        ...

    def fetch(self, scope: str, now: datetime) -> ProviderResponse: ...
