"""Sync module interfaces and registry for Spoticraft."""

from __future__ import annotations

from typing import Dict, Iterable

from .base import SyncContext, SyncModule, SyncModuleFactory
from .playlist_mirror import PlaylistMirrorModule

__all__ = [
    "SyncContext",
    "SyncModule",
    "SyncModuleFactory",
    "ModuleRegistry",
    "default_registry",
]


class ModuleRegistry:
    """Registry for available sync module implementations."""

    def __init__(self) -> None:
        self._registry: Dict[str, SyncModuleFactory] = {}

    def register(self, module_type: str, factory: SyncModuleFactory) -> None:
        if module_type in self._registry:
            raise ValueError(f"Module type already registered: {module_type}")
        self._registry[module_type] = factory

    def get(self, module_type: str) -> SyncModuleFactory:
        try:
            return self._registry[module_type]
        except KeyError as exc:
            raise KeyError(f"Unknown module type: {module_type}") from exc

    def types(self) -> Iterable[str]:
        return self._registry.keys()


# Singleton registry used across the app for now.
default_registry = ModuleRegistry()
default_registry.register("playlist_mirror", PlaylistMirrorModule)
