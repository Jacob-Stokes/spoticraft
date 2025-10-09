"""Playlist mirror sync module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ValidationError

from ..services.spotify_client import SpotifyRateLimitError, SpotifyService
from .base import SyncContext, SyncModule


class PlaylistResolverConfig(BaseModel):
    kind: str
    pattern: Optional[str] = None
    name: Optional[str] = None
    playlist_id: Optional[str] = Field(default=None, alias="id")
    public: bool = False
    description: Optional[str] = None
    max_tracks: Optional[int] = Field(default=None, ge=1)
    lookback_count: Optional[int] = Field(default=None, ge=1)
    lookback_days: Optional[int] = Field(default=None, ge=1)
    full_scan: bool = False
    scan_direction: Literal["oldest", "newest"] = "oldest"


class PlaylistMirrorOptions(BaseModel):
    source: PlaylistResolverConfig
    targets: List[PlaylistResolverConfig]
    deduplicate: bool = True
    max_tracks: Optional[int] = None


@dataclass
class TargetPlaylist:
    id: str
    name: str
    resolver: PlaylistResolverConfig


class PlaylistMirrorModule(SyncModule):
    """Mirror tracks from a source into one or more target playlists."""

    def __init__(self, config):
        self.config = config
        try:
            self.options = PlaylistMirrorOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(f"Invalid playlist mirror options: {exc}") from exc
        self.last_run_summary: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def run(self, context: SyncContext) -> None:
        logger = context.logger
        service = context.spotify
        self._init_summary()
        if service is None:
            logger.error(
                "playlist_mirror.no_spotify_client",
                message="Spotify client unavailable; ensure credentials are configured.",
            )
            self._update_summary(status="failed", reason="no_spotify_client")
            return

        logger.info("playlist_mirror.start")

        try:
            source_track_ids = self._collect_source_tracks(service, context)
        except SpotifyRateLimitError as exc:
            logger.warning("playlist_mirror.rate_limited", phase="source", retry_after=exc.retry_after)
            self._update_summary(status="rate_limited", reason="source_rate_limited", retry_after=exc.retry_after)
            return
        except Exception as exc:  # pragma: no cover - depends on API behaviour
            logger.exception("playlist_mirror.source_failed", error=str(exc))
            self._update_summary(status="failed", reason="source_failed")
            return

        if not source_track_ids:
            logger.info("playlist_mirror.no_source_tracks", message="No tracks to mirror.")
            self._update_summary(status="idle", total_source=0, reason="no_source_tracks")
            return

        self._update_summary(total_source=len(source_track_ids))
        tracks_to_process = self._filter_new_tracks(
            source_track_ids,
            context,
            direction=self.options.source.scan_direction,
        )
        self._update_summary(processed=len(tracks_to_process))

        try:
            targets = self._resolve_targets(service, context)
        except SpotifyRateLimitError as exc:
            logger.warning("playlist_mirror.rate_limited", phase="target_resolve", retry_after=exc.retry_after)
            self._update_summary(status="rate_limited", reason="target_resolution_rate_limited", retry_after=exc.retry_after)
            return
        except Exception as exc:
            logger.exception("playlist_mirror.target_resolution_failed", error=str(exc))
            self._update_summary(status="failed", reason="target_resolution_failed")
            return

        self._update_summary(targets=len(targets))
        if not tracks_to_process:
            logger.info("playlist_mirror.no_new_tracks", reason="cursor_up_to_date")
            self._update_summary(status="up_to_date", reason="cursor_up_to_date")
        else:
            rate_limited = False
            for target in targets:
                try:
                    self._sync_target(service, target, tracks_to_process, source_track_ids, context)
                except SpotifyRateLimitError as exc:
                    logger.warning("playlist_mirror.rate_limited", phase="target_sync", target_id=target.id, retry_after=exc.retry_after)
                    self._update_summary(status="rate_limited", reason="target_sync_rate_limited", retry_after=exc.retry_after)
                    rate_limited = True
                    break
            if rate_limited:
                return
            added_total = int(self.last_run_summary.get("added", 0))
            if added_total > 0:
                self._update_summary(status="success")
            else:
                self._update_summary(status="noop", reason="no_new_tracks_after_deduplicate")

        if context.state and source_track_ids:
            if (self.options.source.scan_direction or "oldest").lower() == "newest":
                cursor_value = source_track_ids[0]
            else:
                cursor_value = source_track_ids[-1]
            context.state.set_last_processed_track_id(cursor_value)

        logger.info("playlist_mirror.completed", targets=len(targets), processed=len(tracks_to_process))

    # ------------------------------------------------------------------
    # Source helpers
    # ------------------------------------------------------------------
    def _collect_source_tracks(self, service: SpotifyService, context: SyncContext) -> List[str]:
        source = self.options.source
        last_processed_id = None
        if context.state:
            last_processed_id = context.state.last_processed_track_id
        if source.kind == "saved_tracks":
            max_tracks = source.max_tracks
            if max_tracks is None:
                max_tracks = self.options.max_tracks
            return service.get_saved_tracks(
                max_tracks=max_tracks,
                lookback_count=source.lookback_count,
                lookback_days=source.lookback_days,
                full_scan=source.full_scan,
                last_processed_id=last_processed_id,
                direction=source.scan_direction,
            )
        if source.kind == "playlist_id" and source.playlist_id:
            return service.get_playlist_tracks(source.playlist_id)
        if source.kind == "playlist_name" and source.name:
            playlist = service.find_playlist_by_name(source.name)
            if not playlist:
                return []
            return service.get_playlist_tracks(playlist["id"])
        raise ValueError(f"Unsupported source kind: {source.kind}")

    # ------------------------------------------------------------------
    # Target helpers
    # ------------------------------------------------------------------
    def _resolve_targets(self, service: SpotifyService, context: SyncContext) -> List[TargetPlaylist]:
        cache_store: Dict[str, str] = {}
        if context.state is not None:
            cache_store = context.state.data.setdefault("playlist_cache", {})
        targets: List[TargetPlaylist] = []
        for resolver in self.options.targets:
            if resolver.kind == "playlist_id" and resolver.playlist_id:
                playlist = service.client.playlist(resolver.playlist_id)
                targets.append(
                    TargetPlaylist(id=playlist["id"], name=playlist["name"], resolver=resolver)
                )
            elif resolver.kind == "playlist_name" and resolver.name:
                cache_key = f"name::{resolver.name.strip().lower()}"
                playlist = self._get_or_create_playlist(
                    service,
                    resolver.name,
                    public=resolver.public,
                    description=resolver.description,
                    cache_key=cache_key,
                    cache_store=cache_store,
                    state=context.state,
                )
                targets.append(
                    TargetPlaylist(id=playlist["id"], name=playlist["name"], resolver=resolver)
                )
            elif resolver.kind == "playlist_pattern" and resolver.pattern:
                playlist_name = service.format_pattern(resolver.pattern)
                cache_key = f"pattern::{resolver.pattern}::{playlist_name.strip().lower()}"
                playlist = self._get_or_create_playlist(
                    service,
                    playlist_name,
                    public=resolver.public,
                    description=resolver.description,
                    cache_key=cache_key,
                    cache_store=cache_store,
                    state=context.state,
                )
                targets.append(
                    TargetPlaylist(id=playlist["id"], name=playlist["name"], resolver=resolver)
                )
            else:
                raise ValueError(f"Unsupported target resolver: {resolver.kind}")
        return targets

    def _get_or_create_playlist(
        self,
        service: SpotifyService,
        name: str,
        *,
        public: bool,
        description: Optional[str],
        cache_key: str,
        cache_store: Dict[str, str],
        state,
    ) -> dict:
        cached_id = cache_store.get(cache_key)
        if cached_id:
            try:
                playlist = service.client.playlist(cached_id)
                return playlist
            except Exception:
                cache_store.pop(cache_key, None)

        playlist = service.ensure_playlist(
            name,
            public=public,
            description=description,
        )
        cache_store[cache_key] = playlist["id"]
        if state is not None:
            state._dirty = True
        return playlist

    def _filter_new_tracks(
        self,
        source_track_ids: List[str],
        context: SyncContext,
        *,
        direction: Optional[str] = None,
    ) -> List[str]:
        state = context.state
        normalised_direction = (direction or "oldest").lower()

        if not state or not state.last_processed_track_id:
            if normalised_direction == "newest":
                return list(reversed(source_track_ids))
            return source_track_ids

        last_id = state.last_processed_track_id
        try:
            index = source_track_ids.index(last_id)
        except ValueError:
            context.logger.warning(
                "playlist_mirror.cursor_missing",
                last_processed_id=last_id,
                message="Previous cursor not found; processing all tracks.",
            )
            return source_track_ids
        if normalised_direction == "newest":
            # list is newest-first; new tracks appear before the cursor.
            # Return oldest-to-newest order for consistent playlist appends.
            return list(reversed(source_track_ids[:index]))
        return source_track_ids[index + 1 :]

    def _sync_target(
        self,
        service: SpotifyService,
        target: TargetPlaylist,
        tracks_to_process: List[str],
        source_track_ids: List[str],
        context: SyncContext,
    ) -> None:
        logger = context.logger.bind(target_id=target.id, target_name=target.name)

        if not tracks_to_process:
            logger.info("playlist_mirror.target_skipped", reason="no_source_tracks")
            return

        tracks_to_add = list(tracks_to_process)
        state_cursor = context.state.last_processed_track_id if context.state else None
        should_deduplicate = self.options.deduplicate and state_cursor is None

        if should_deduplicate:
            existing = service.get_playlist_tracks(target.id)
            existing_set = set(existing)
            tracks_to_add = [tid for tid in tracks_to_add if tid not in existing_set]
        elif self.options.deduplicate:
            logger.debug("playlist_mirror.deduplicate_skipped", reason="cursor_active")

        if not tracks_to_add:
            logger.info("playlist_mirror.target_skipped", reason="no_new_tracks")
            return

        added = service.add_tracks(target.id, tracks_to_add)
        self._increment_summary("added", added)
        logger.info(
            "playlist_mirror.target_synced",
            added=added,
            requested=len(tracks_to_add),
            total_source=len(source_track_ids),
        )

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------
    def _init_summary(self) -> None:
        self.last_run_summary = {
            "status": "running",
            "processed": 0,
            "targets": 0,
            "total_source": 0,
            "added": 0,
        }

    def _update_summary(self, **fields) -> None:
        if not isinstance(self.last_run_summary, dict):
            self._init_summary()
        self.last_run_summary.update(fields)

    def _increment_summary(self, key: str, value: int) -> None:
        if not isinstance(self.last_run_summary, dict):
            self._init_summary()
        current = self.last_run_summary.get(key, 0)
        if not isinstance(current, int):
            current = 0
        self.last_run_summary[key] = current + value
