"""Persistent state management for Spoticraft syncs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ConfigPaths, GlobalConfig, SyncConfig

STATE_VERSION = 1
RUN_HISTORY_LIMIT = 20


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncState:
    """Represents on-disk state for a single sync."""

    path: Path
    data: Dict[str, Any] = field(default_factory=dict)
    _dirty: bool = field(default=False, init=False, repr=False)

    def save(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": STATE_VERSION,
            "updated_at": _utcnow_iso(),
            **self.data,
        }
        _ensure_parent(self.path)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        self._dirty = False

    @property
    def last_processed_track_id(self) -> Optional[str]:
        return self.data.get("last_processed_track_id")

    def set_last_processed_track_id(self, track_id: Optional[str]) -> None:
        if track_id is None:
            if "last_processed_track_id" in self.data:
                self.data.pop("last_processed_track_id", None)
                self.data.pop("last_processed_at", None)
                self._dirty = True
            return
        if self.data.get("last_processed_track_id") == track_id:
            return
        self.data["last_processed_track_id"] = track_id
        self.data["last_processed_at"] = _utcnow_iso()
        self._dirty = True

    # ------------------------------------------------------------------
    # Run history helpers
    # ------------------------------------------------------------------
    def _ensure_run_history(self) -> list:
        history = self.data.get("run_history")
        if not isinstance(history, list):
            history = []
            self.data["run_history"] = history
            self._dirty = True
        return history

    def _trim_run_history(self) -> None:
        history = self.data.get("run_history")
        if isinstance(history, list) and len(history) > RUN_HISTORY_LIMIT:
            del history[:-RUN_HISTORY_LIMIT]
            self._dirty = True

    def begin_run(self, run_id: str, started_at: Optional[str] = None) -> None:
        history = self._ensure_run_history()
        history.append(
            {
                "id": run_id,
                "status": "running",
                "started_at": started_at or _utcnow_iso(),
            }
        )
        self._trim_run_history()
        self._dirty = True

    def complete_run(
        self,
        run_id: str,
        status: str,
        completed_at: Optional[str] = None,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        history = self._ensure_run_history()
        record = None
        for candidate in reversed(history):
            if candidate.get("id") == run_id:
                record = candidate
                break

        if record is None:
            record = {
                "id": run_id,
                "started_at": completed_at or _utcnow_iso(),
            }
            history.append(record)

        record["status"] = status
        record["completed_at"] = completed_at or _utcnow_iso()

        if error is not None:
            record["error"] = error
        else:
            record.pop("error", None)

        if details is not None:
            record["details"] = details
        elif "details" in record:
            record.pop("details", None)

        self._trim_run_history()
        self._dirty = True


def load_state(path: Path) -> SyncState:
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle) or {}
        # Remove metadata keys we manage separately
        data = {k: v for k, v in payload.items() if k not in {"version", "updated_at"}}
        return SyncState(path=path, data=data)
    return SyncState(path=path)


def state_path_for_sync(
    paths: ConfigPaths,
    global_config: GlobalConfig,
    sync_config: SyncConfig,
) -> Path:
    storage_root = Path(global_config.runtime.storage_dir).expanduser()
    if sync_config.state_file:
        state_path = Path(sync_config.state_file)
        if not state_path.is_absolute():
            state_path = storage_root / state_path
    else:
        state_path = storage_root / f"{sync_config.id}.json"
    return state_path
