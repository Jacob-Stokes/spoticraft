"""Last.fm top tracks sync module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

from .base import SyncContext, SyncModule
from .playlist_mirror import PlaylistResolverConfig
from ..services.spotify_client import SpotifyService


class LastFmTopTracksOptions(BaseModel):
    playlist: PlaylistResolverConfig
    limit: int = Field(default=10, ge=1, le=100)
    period: str = Field(default="7day")
    clear_before_add: bool = Field(default=True)


@dataclass
class LastFmTopTracksModule(SyncModule):
    """Populate a playlist with Last.fm top tracks."""

    config: any

    def __init__(self, config):
        self.config = config
        try:
            self.options = LastFmTopTracksOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(f"Invalid last.fm top tracks options: {exc}") from exc
        self.last_run_summary: dict = {}

    def run(self, context: SyncContext) -> None:
        logger = context.logger
        service = context.spotify
        if service is None:
            logger.error("lastfm.no_spotify_client", message="Spotify client unavailable.")
            return

        credentials = context.global_config
        if not credentials or not credentials.lastfm:
            logger.error("lastfm.missing_credentials", message="Last.fm settings not configured.")
            return

        api_key = credentials.lastfm.api_key
        username = credentials.lastfm.username
        if not api_key or not username:
            logger.error("lastfm.invalid_credentials", message="API key and username required.")
            return

        playlist_id = self._resolve_playlist(service)
        logger.info("lastfm.sync.start", playlist_id=playlist_id)

        tracks = self._fetch_lastfm_tracks(api_key, username)
        if not tracks:
            logger.warning("lastfm.sync.no_tracks")
            self._update_summary(status="noop", added=0)
            return

        spotify_track_ids: List[str] = []
        for track in tracks:
            spotify_id = service.search_track(track["name"], track["artist"])
            if spotify_id:
                spotify_track_ids.append(spotify_id)
            else:
                logger.warning(
                    "lastfm.sync.search_miss",
                    track=track["name"],
                    artist=track["artist"],
                )

        if not spotify_track_ids:
            logger.warning("lastfm.sync.no_matches")
            self._update_summary(status="failed", reason="no_spotify_matches")
            return

        state = context.state
        previous = state.data.get("last_tracks", [])
        if previous == spotify_track_ids:
            logger.info("lastfm.sync.unchanged", message="Playlist already up to date.")
            self._update_summary(status="unchanged", added=0)
            return

        if self.options.clear_before_add:
            service.replace_tracks(playlist_id, spotify_track_ids)
        else:
            service.replace_tracks(playlist_id, [])
            service.add_tracks(playlist_id, spotify_track_ids)

        state.data["last_tracks"] = spotify_track_ids
        state._dirty = True

        logger.info("lastfm.sync.completed", added=len(spotify_track_ids))
        self._update_summary(status="success", added=len(spotify_track_ids))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _fetch_lastfm_tracks(self, api_key: str, username: str) -> List[dict]:
        params = {
            "method": "user.gettoptracks",
            "user": username,
            "api_key": api_key,
            "format": "json",
            "period": self.options.period,
            "limit": self.options.limit,
        }

        response = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        tracks = []
        for item in payload.get("toptracks", {}).get("track", []):
            tracks.append(
                {
                    "name": item.get("name", ""),
                    "artist": item.get("artist", {}).get("name", ""),
                }
            )
        return tracks

    def _resolve_playlist(self, service: SpotifyService) -> str:
        resolver = self.options.playlist
        if resolver.kind == "playlist_id" and resolver.playlist_id:
            return resolver.playlist_id
        if resolver.kind == "playlist_name" and resolver.name:
            playlist = service.find_playlist_by_name(resolver.name)
            if not playlist:
                raise ValueError(f"Playlist '{resolver.name}' not found")
            return playlist["id"]
        if resolver.kind == "playlist_pattern" and resolver.pattern:
            name = service.format_pattern(resolver.pattern)
            playlist = service.ensure_playlist(
                name,
                public=resolver.public,
                description=resolver.description,
            )
            return playlist["id"]
        raise ValueError(f"Unsupported playlist resolver: {resolver.kind}")

    def _update_summary(self, **fields) -> None:
        if not isinstance(self.last_run_summary, dict):
            self.last_run_summary = {}
        self.last_run_summary.update(fields)
