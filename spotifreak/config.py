"""Configuration models and helpers for Spotifreak."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_SPOTIFY_PLACEHOLDER = "SET_ME"


@dataclass(frozen=True)
class BootstrapReport:
    """Summary of files/directories created during initialisation."""

    base_created: bool
    state_dir_created: bool
    syncs_dir_created: bool
    global_config_created: bool
    global_config_overwritten: bool


class ConfigError(RuntimeError):
    """Raised when configuration files cannot be parsed or are invalid."""


@dataclass(frozen=True)
class ConfigPaths:
    """Resolved filesystem locations used by the application."""

    base_dir: Path
    global_config: Path
    syncs_dir: Path

    @classmethod
    def default(cls) -> "ConfigPaths":
        """Return default locations under the user's home directory."""

        base = Path.home() / ".spotifreak"
        return cls.from_base_dir(base)

    @classmethod
    def from_base_dir(cls, base_dir: Path) -> "ConfigPaths":
        """Construct paths using ``base_dir`` as root."""

        base_dir = base_dir.expanduser()
        return cls(
            base_dir=base_dir,
            global_config=base_dir / "config.yml",
            syncs_dir=base_dir / "syncs",
        )

    @property
    def state_dir(self) -> Path:
        """Default directory for sync state files."""

        return self.base_dir / "state"

    def resolve_state_path(self, state_file: Optional[str]) -> Path:
        """Resolve a relative state file path against ``base_dir``.

        When ``state_file`` is ``None`` the default path ``<base>/<sync_id>.json``
        should be determined by the caller (requires sync identifier).
        """

        if state_file is None:
            raise ValueError("state_file must be provided for explicit resolution")
        candidate = Path(state_file)
        return candidate if candidate.is_absolute() else self.base_dir / candidate


class RetryPolicy(BaseModel):
    """Default retry behaviour for sync executions."""

    attempts: int = Field(default=3, ge=0)
    backoff_seconds: float = Field(default=30.0, ge=0.0)

    model_config = ConfigDict(extra="forbid")


class SpotifySettings(BaseModel):
    """Spotify API credentials."""

    client_id: str
    client_secret: str
    redirect_uri: str = Field(default="http://localhost:8888/callback")
    scopes: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class LastFMSettings(BaseModel):
    """Optional Last.fm API credentials."""

    api_key: str
    api_secret: str
    username: str

    model_config = ConfigDict(extra="forbid")


class RuntimeSettings(BaseModel):
    """Runtime-level defaults."""

    timezone: str = Field(default="UTC")
    storage_dir: Path = Field(default_factory=lambda: ConfigPaths.default().state_dir)
    log_level: str = Field(default="INFO")
    default_retry: Optional[RetryPolicy] = None

    model_config = ConfigDict(extra="forbid")


class SupervisorSettings(BaseModel):
    """Supervisor process configuration."""

    ipc_socket: Path = Field(default_factory=lambda: ConfigPaths.default().base_dir / "ipc.sock")
    hot_reload: bool = Field(default=True)

    model_config = ConfigDict(extra="forbid")


class GlobalConfig(BaseModel):
    """Top-level configuration file model."""

    spotify: SpotifySettings
    lastfm: Optional[LastFMSettings] = None
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    supervisor: SupervisorSettings = Field(default_factory=SupervisorSettings)

    model_config = ConfigDict(extra="forbid")

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the configured retry policy with defaults applied."""

        if self.runtime.default_retry is not None:
            return self.runtime.default_retry
        return RetryPolicy()


class SyncSchedule(BaseModel):
    """Scheduling information for a sync."""

    interval: Optional[str] = None
    cron: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_schedule(self) -> "SyncSchedule":  # pragma: no cover - simple validation
        if not (self.interval or self.cron):
            raise ValueError("Schedule must define either 'interval' or 'cron'.")
        return self


class SyncConfig(BaseModel):
    """Definition of a single sync job."""

    id: str
    type: str
    schedule: SyncSchedule
    state_file: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:  # pragma: no cover - depends on invalid input
        raise ConfigError(f"Failed to parse YAML file: {path}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Expected mapping at top level of {path}")
    return data


def load_global_config(path: Path) -> GlobalConfig:
    """Load and validate the global configuration file."""

    payload = _read_yaml(path)
    return GlobalConfig.model_validate(payload)


def iter_sync_config_paths(syncs_dir: Path) -> Iterable[Path]:
    """Yield YAML files representing sync definitions."""

    if not syncs_dir.exists():
        return []
    return sorted(p for p in syncs_dir.iterdir() if p.suffix in {".yml", ".yaml"} and p.is_file())


def load_sync_configs(syncs_dir: Path) -> List[SyncConfig]:
    """Load all sync configurations from the given directory."""

    configs: List[SyncConfig] = []
    for path in iter_sync_config_paths(syncs_dir):
        payload = _read_yaml(path)
        try:
            configs.append(SyncConfig.model_validate(payload))
        except Exception as exc:  # pragma: no cover - validation errors depend on user input
            raise ConfigError(f"Invalid sync configuration: {path}") from exc
    return configs


def _default_global_config(paths: ConfigPaths) -> Dict[str, Any]:
    """Dictionary representing the starter global configuration."""

    storage_dir = paths.state_dir
    ipc_socket = paths.base_dir / "ipc.sock"

    return {
        "spotify": {
            "client_id": DEFAULT_SPOTIFY_PLACEHOLDER,
            "client_secret": DEFAULT_SPOTIFY_PLACEHOLDER,
            "redirect_uri": "http://localhost:8888/callback",
            "scopes": [
                "user-library-read",
                "playlist-read-private",
                "playlist-modify-private",
                "playlist-modify-public",
            ],
        },
        "runtime": {
            "timezone": "UTC",
            "storage_dir": str(storage_dir),
            "log_level": "INFO",
            "default_retry": {
                "attempts": 3,
                "backoff_seconds": 30,
            },
        },
        "supervisor": {
            "ipc_socket": str(ipc_socket),
            "hot_reload": True,
        },
    }


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


def bootstrap(paths: ConfigPaths, overwrite: bool = False) -> BootstrapReport:
    """Ensure configuration directories/files exist.

    Parameters
    ----------
    paths:
        Target filesystem layout.
    overwrite:
        When ``True`` the global config file is re-written even if it already exists.
    """

    base_created = False
    state_dir_created = False
    syncs_dir_created = False
    global_config_created = False
    global_config_overwritten = False

    if not paths.base_dir.exists():
        paths.base_dir.mkdir(parents=True, exist_ok=True)
        base_created = True

    if not paths.state_dir.exists():
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        state_dir_created = True

    if not paths.syncs_dir.exists():
        paths.syncs_dir.mkdir(parents=True, exist_ok=True)
        syncs_dir_created = True

    existing_global = paths.global_config.exists()
    if not existing_global or overwrite:
        _write_yaml(paths.global_config, _default_global_config(paths))
        global_config_created = True
        global_config_overwritten = existing_global and overwrite

    return BootstrapReport(
        base_created=base_created,
        state_dir_created=state_dir_created,
        syncs_dir_created=syncs_dir_created,
        global_config_created=global_config_created,
        global_config_overwritten=global_config_overwritten,
    )
