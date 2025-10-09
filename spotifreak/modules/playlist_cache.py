"""Playlist cache sync module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

from pydantic import BaseModel, Field

from .base import SyncContext, SyncModule


class PlaylistCacheOptions(BaseModel):
    include_public: bool = Field(default=True)
    include_private: bool = Field(default=True)
    include_collaborative: bool = Field(default=True)


@dataclass
class PlaylistCacheModule(SyncModule):
    """Enumerate Spotify playlists and populate a shared cache."""

    config: any

    def __init__(self, config):
        self.config = config
        self.options = PlaylistCacheOptions.model_validate(config.options or {})
        self.last_run_summary: dict = {}

    def run(self, context: SyncContext) -> None:
        logger = context.logger
        service = context.spotify
        state = context.state

        if service is None or state is None:
            logger.error("playlist_cache.no_spotify_client")
            return

        self.last_run_summary = {"status": "running"}
        logger.info("playlist_cache.start")

        playlists = service.list_all_playlists()
        logger.info("playlist_cache.discovered", count=len(playlists))

        filtered = self._filter_playlists(playlists)
        logger.info("playlist_cache.filtered", count=len(filtered))

        entries: List[Dict[str, object]] = []
        for item in filtered:
            owner = item.get("owner") or {}
            entry = {
                "id": item.get("id"),
                "uri": item.get("uri"),
                "href": item.get("href"),
                "name": item.get("name", ""),
                "owner_id": owner.get("id"),
                "public": item.get("public"),
                "collaborative": item.get("collaborative"),
                "snapshot_id": item.get("snapshot_id"),
            }
            entries.append(entry)

        state.data["last_refreshed"] = datetime.now(timezone.utc).isoformat()
        state.data["playlists"] = entries
        state._dirty = True

        logger.info("playlist_cache.completed", stored=len(entries))
        self.last_run_summary = {
            "status": "success",
            "stored": len(entries),
        }

    def _filter_playlists(self, playlists: List[dict]) -> List[dict]:
        filtered: List[dict] = []
        for item in playlists:
            if not self.options.include_public and bool(item.get("public")):
                continue
            if not self.options.include_private and item.get("public") is False:
                continue
            if not self.options.include_collaborative and bool(item.get("collaborative")):
                continue
            filtered.append(item)
        return filtered
