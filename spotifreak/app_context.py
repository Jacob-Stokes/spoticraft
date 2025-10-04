"""Shared application context for Spotifreak CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import ConfigPaths, GlobalConfig, SyncConfig, load_global_config, load_sync_configs


@dataclass
class AppContext:
    """Container for resolved configuration used by CLI commands."""

    paths: ConfigPaths
    global_config: GlobalConfig
    syncs: List[SyncConfig]


def determine_paths(config_dir: Optional[Path]) -> ConfigPaths:
    """Resolve configuration paths based on optional CLI override."""

    return ConfigPaths.from_base_dir(config_dir) if config_dir else ConfigPaths.default()


def load_context(paths: ConfigPaths) -> AppContext:
    """Load global configuration and sync definitions from disk."""

    global_config = load_global_config(paths.global_config)
    syncs = load_sync_configs(paths.syncs_dir)

    return AppContext(paths=paths, global_config=global_config, syncs=syncs)
