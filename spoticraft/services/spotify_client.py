"""Thin wrapper around Spotipy with convenience helpers."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Optional

try:
    import spotipy
except ModuleNotFoundError as exc:  # pragma: no cover - dependency may be missing in sandbox
    raise RuntimeError(
        "Spotipy is required for Spotify operations. Install via 'pip install spotipy'."
    ) from exc


BATCH_SIZE = 100


class SpotifyService:
    """High-level Spotify helpers built on top of Spotipy."""

    def __init__(self, client: spotipy.Spotify) -> None:
        self.client = client
        self._current_user: Optional[dict] = None
        self._playlists_cache: Optional[List[dict]] = None

    # ------------------------------------------------------------------
    # User & playlist discovery utilities
    # ------------------------------------------------------------------
    @property
    def current_user(self) -> dict:
        if self._current_user is None:
            self._current_user = self.client.current_user()
        return self._current_user

    @property
    def user_id(self) -> str:
        return self.current_user["id"]

    def _fetch_all_playlists(self) -> List[dict]:
        playlists: List[dict] = []
        results = self.client.current_user_playlists(limit=50)
        while results:
            playlists.extend(results["items"])
            if results["next"]:
                results = self.client.next(results)
            else:
                break
        return playlists

    def ensure_playlist(self, name: str, public: bool = False, description: Optional[str] = None) -> dict:
        playlist = self.find_playlist_by_name(name)
        if playlist:
            return playlist
        playlist = self.client.user_playlist_create(
            user=self.user_id,
            name=name,
            public=public,
            description=description or ""
        )
        # refresh cache
        self._playlists_cache = None
        return playlist

    def find_playlist_by_name(self, name: str) -> Optional[dict]:
        name_lower = name.strip().lower()
        if self._playlists_cache is None:
            self._playlists_cache = self._fetch_all_playlists()
        for playlist in self._playlists_cache:
            if playlist["name"].strip().lower() == name_lower:
                return playlist
        return None

    # ------------------------------------------------------------------
    # Track fetching helpers
    # ------------------------------------------------------------------
    def get_saved_tracks(
        self,
        *,
        max_tracks: Optional[int] = None,
        lookback_count: Optional[int] = None,
        lookback_days: Optional[int] = None,
        full_scan: bool = False,
        last_processed_id: Optional[str] = None,
        direction: str = "oldest",
    ) -> List[str]:
        """Return saved track IDs honoring optional scan constraints."""

        def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                if value.endswith("Z"):
                    value = value[:-1] + "+00:00"
                return datetime.fromisoformat(value)
            except ValueError:
                try:
                    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    return None

        max_items = max_tracks
        if lookback_count is not None:
            max_items = min(max_items, lookback_count) if max_items else lookback_count

        page_limit = 50
        if max_items is not None:
            page_limit = max(1, min(page_limit, max_items))
        elif lookback_count is not None:
            page_limit = max(1, min(page_limit, lookback_count))

        cutoff: Optional[datetime] = None
        if lookback_days and lookback_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        collected: List[tuple[str, Optional[datetime]]] = []
        results = self.client.current_user_saved_tracks(limit=page_limit)

        halt_scan = False
        while results and not halt_scan:
            for item in results.get("items", []):
                track = item.get("track")
                track_id = track.get("id") if track else None
                if not track_id:
                    continue

                if not full_scan and last_processed_id and track_id == last_processed_id:
                    halt_scan = True
                    break

                added_at = parse_timestamp(item.get("added_at"))
                if cutoff and added_at and added_at < cutoff:
                    halt_scan = True
                    break

                collected.append((track_id, added_at))

                if lookback_count and len(collected) >= lookback_count:
                    halt_scan = True
                    break
                if max_tracks and len(collected) >= max_tracks:
                    halt_scan = True
                    break

            if halt_scan or not results.get("next"):
                break
            results = self.client.next(results)

        normalized_direction = (direction or "oldest").lower()
        if normalized_direction not in {"oldest", "newest"}:
            normalized_direction = "oldest"

        if normalized_direction == "oldest":
            collected = list(reversed(collected))

        return [track_id for track_id, _ in collected]

    def get_playlist_tracks(self, playlist_id: str) -> List[str]:
        track_ids: List[str] = []
        results = self.client.playlist_items(playlist_id, fields="items(track(id)),next")
        while results:
            track_ids.extend(
                track["track"]["id"]
                for track in results["items"]
                if track.get("track") and track["track"].get("id")
            )
            if results["next"]:
                results = self.client.next(results)
            else:
                break
        return track_ids

    # ------------------------------------------------------------------
    # Mutating helpers
    # ------------------------------------------------------------------
    def add_tracks(self, playlist_id: str, track_ids: Iterable[str]) -> int:
        track_list = list(track_ids)
        if not track_list:
            return 0
        total = 0
        for offset in range(0, len(track_list), BATCH_SIZE):
            batch = track_list[offset : offset + BATCH_SIZE]
            self.client.playlist_add_items(playlist_id, batch)
            total += len(batch)
        return total

    def remove_tracks(self, playlist_id: str, track_ids: Iterable[str]) -> int:
        track_list = list(track_ids)
        if not track_list:
            return 0
        total = 0
        for offset in range(0, len(track_list), BATCH_SIZE):
            batch = track_list[offset : offset + BATCH_SIZE]
            self.client.playlist_remove_all_occurrences_of_items(playlist_id, batch)
            total += len(batch)
        return total

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def format_pattern(pattern: str, now: Optional[datetime] = None) -> str:
        now = now or datetime.now()
        replacements = {
            "${month_abbr}": now.strftime("%b").upper(),
            "${month_full}": now.strftime("%B"),
            "${year_short}": now.strftime("%y"),
            "${year_full}": now.strftime("%Y"),
            "${weekday}": now.strftime("%A"),
        }
        formatted = pattern
        for key, value in replacements.items():
            formatted = formatted.replace(key, value)
        return formatted
