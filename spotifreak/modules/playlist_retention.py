"""Playlist retention module that archives and prunes playlists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Set

from pydantic import BaseModel, Field, ValidationError

from .base import SyncContext, SyncModule
from .playlist_mirror import PlaylistResolverConfig
from ..services.spotify_client import SpotifyService


class PlaylistRetentionOptions(BaseModel):
    source: PlaylistResolverConfig
    archive: Optional[PlaylistResolverConfig] = None
    retention_days: Optional[int] = Field(default=None, ge=1)
    max_tracks: Optional[int] = Field(default=None, ge=1)
    min_tracks: Optional[int] = Field(default=None, ge=0)


@dataclass
class PlaylistRetentionModule(SyncModule):
    """Retain recent tracks in a playlist and archive/prune older entries."""

    config: any

    def __init__(self, config):
        self.config = config
        try:
            self.options = PlaylistRetentionOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(f"Invalid playlist retention options: {exc}") from exc
        self.last_run_summary: dict = {}

    def run(self, context: SyncContext) -> None:
        logger = context.logger
        service = context.spotify
        if service is None:
            logger.error(
                "playlist_retention.no_spotify_client",
                message="Spotify client unavailable; ensure credentials are configured.",
            )
            return

        logger.info("playlist_retention.start")

        source_playlist_id = self._resolve_source_playlist(service)
        archive_playlist_id = self._resolve_archive_playlist(service) if self.options.archive else None

        tracks = service.get_playlist_items_with_added_at(source_playlist_id)
        logger.info("playlist_retention.source_fetched", count=len(tracks))

        to_remove = self._determine_tracks_to_remove(tracks)
        if not to_remove:
            logger.info("playlist_retention.no_changes", message="Retention criteria satisfied.")
            self._update_summary(status="noop", removed=0, retained=len(tracks))
            return

        removal_ids = [track["id"] for track in to_remove]
        removal_count = len(removal_ids)

        if archive_playlist_id:
            added = service.add_tracks(archive_playlist_id, removal_ids)
            logger.info(
                "playlist_retention.archived",
                added=added,
                archive_playlist=archive_playlist_id,
            )

        service.remove_tracks(source_playlist_id, removal_ids)
        logger.info(
            "playlist_retention.pruned",
            removed=removal_count,
            remaining=max(len(tracks) - removal_count, 0),
        )

        self._update_summary(
            status="success",
            removed=removal_count,
            archived=removal_count if archive_playlist_id else 0,
            retained=max(len(tracks) - removal_count, 0),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_source_playlist(self, service: SpotifyService) -> str:
        resolver = self.options.source
        if resolver.kind == "playlist_id" and resolver.playlist_id:
            return resolver.playlist_id
        if resolver.kind == "playlist_name" and resolver.name:
            playlist = service.find_playlist_by_name(resolver.name)
            if not playlist:
                raise ValueError(f"Source playlist '{resolver.name}' not found")
            return playlist["id"]
        raise ValueError(f"Unsupported source resolver: {resolver.kind}")

    def _resolve_archive_playlist(self, service: SpotifyService) -> str:
        resolver = self.options.archive
        if resolver is None:
            return ""
        if resolver.kind == "playlist_id" and resolver.playlist_id:
            return resolver.playlist_id
        if resolver.kind == "playlist_name" and resolver.name:
            playlist = service.ensure_playlist(
                resolver.name,
                public=resolver.public,
                description=resolver.description,
            )
            return playlist["id"]
        if resolver.kind == "playlist_pattern" and resolver.pattern:
            name = service.format_pattern(resolver.pattern)
            playlist = service.ensure_playlist(
                name,
                public=resolver.public,
                description=resolver.description,
            )
            return playlist["id"]
        raise ValueError(f"Unsupported archive resolver: {resolver.kind}")

    def _determine_tracks_to_remove(self, tracks: List[dict]) -> List[dict]:
        if not tracks:
            return []

        removal: List[dict] = []
        retention_cutoff = None
        if self.options.retention_days:
            retention_cutoff = datetime.now(timezone.utc) - timedelta(days=self.options.retention_days)

        def parse_added_at(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                if value.endswith("Z"):
                    value = value[:-1] + "+00:00"
                return datetime.fromisoformat(value)
            except ValueError:
                return None

        for track in tracks:
            added_at = parse_added_at(track.get("added_at"))
            if retention_cutoff and added_at and added_at < retention_cutoff:
                removal.append(track)

        if self.options.max_tracks is not None:
            max_tracks = self.options.max_tracks
            if len(tracks) - len(removal) > max_tracks:
                # need to remove oldest beyond max
                sorted_tracks = sorted(tracks, key=lambda t: parse_added_at(t.get("added_at")) or datetime.min)
                keep_set: Set[str] = {t["id"] for t in sorted_tracks[-max_tracks:]}
                for track in sorted_tracks:
                    if track["id"] not in keep_set and track not in removal:
                        removal.append(track)

        if self.options.min_tracks is not None:
            min_tracks = self.options.min_tracks
            remaining = len(tracks) - len(removal)
            if remaining < min_tracks:
                # undo enough removals to satisfy minimum (prioritise newest to keep)
                removal.sort(key=lambda t: parse_added_at(t.get("added_at")) or datetime.min)
                while removal and (len(tracks) - len(removal)) < min_tracks:
                    removal.pop(0)

        # Ensure unique and sorted by original time
        unique: dict[str, dict] = {}
        for track in removal:
            unique[track["id"]] = track

        removal_list = list(unique.values())
        removal_list.sort(key=lambda t: parse_added_at(t.get("added_at")) or datetime.min)
        return removal_list

    def _update_summary(self, **fields) -> None:
        if not isinstance(self.last_run_summary, dict):
            self.last_run_summary = {}
        self.last_run_summary.update(fields)
