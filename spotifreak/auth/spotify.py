"""Spotify API authentication and client helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from ..config import GlobalConfig


@dataclass
class SpotifyClientSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    cache_path: Optional[Path] = None


class SpotifyClientFactory:
    """Factory for building authenticated Spotipy clients."""

    def __init__(self, global_config: GlobalConfig) -> None:
        spotify_settings = global_config.spotify
        if spotify_settings.client_id in {"", "SET_ME"} or spotify_settings.client_secret in {"", "SET_ME"}:
            raise RuntimeError(
                "Spotify credentials are not configured. Update config.yml with real CLIENT_ID/SECRET."
            )
        scopes = spotify_settings.scopes or [
            "playlist-read-private",
            "playlist-modify-private",
            "playlist-modify-public",
        ]

        merged_scope = " ".join(sorted(set(scopes)))
        cache_path = Path(global_config.runtime.storage_dir).expanduser() / "auth_cache"
        cache_path.mkdir(parents=True, exist_ok=True)

        self.settings = SpotifyClientSettings(
            client_id=spotify_settings.client_id,
            client_secret=spotify_settings.client_secret,
            redirect_uri=spotify_settings.redirect_uri,
            scope=merged_scope,
            cache_path=cache_path / "token.json",
        )

    def get_client(self) -> spotipy.Spotify:
        """Return an authenticated Spotipy client."""

        oauth = SpotifyOAuth(
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret,
            redirect_uri=self.settings.redirect_uri,
            scope=self.settings.scope,
            cache_path=str(self.settings.cache_path) if self.settings.cache_path else None,
        )
        return spotipy.Spotify(
            auth_manager=oauth,
            requests_timeout=10,
            retries=0,
            status_retries=0,
            backoff_factor=0.0,
        )
