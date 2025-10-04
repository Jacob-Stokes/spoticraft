"""Base interfaces for Spotifreak sync modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, TYPE_CHECKING

from structlog.stdlib import BoundLogger

from ..config import SyncConfig, ConfigPaths

if TYPE_CHECKING:  # pragma: no cover - typing aid
    from ..services.spotify_client import SpotifyService
    from ..state import SyncState
    from ..config import GlobalConfig


@dataclass
class SyncContext:
    """Runtime context shared with modules when they execute."""

    logger: BoundLogger
    spotify: Optional["SpotifyService"] = None
    state: Optional["SyncState"] = None
    global_config: Optional["GlobalConfig"] = None
    paths: Optional[ConfigPaths] = None


class SyncModule(Protocol):
    """Protocol defining the required behaviour for sync modules."""

    config: SyncConfig

    def run(self, context: SyncContext) -> None:
        """Execute module logic once."""


class SyncModuleFactory(Protocol):
    """Factories create module instances from configuration."""

    def __call__(self, config: SyncConfig) -> SyncModule:
        ...
