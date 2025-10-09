"""Thin wrapper around Spotipy with convenience helpers."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Optional, Dict, Any

try:
    import spotipy
    from spotipy.exceptions import SpotifyException
except ModuleNotFoundError as exc:  # pragma: no cover - dependency may be missing in sandbox
    raise RuntimeError(
        "Spotipy is required for Spotify operations. Install via 'pip install spotipy'."
    ) from exc


BATCH_SIZE = 100


class SpotifyRateLimitError(RuntimeError):
    """Raised when the Spotify API responds with a 429 rate limit."""

    def __init__(self, retry_after: Optional[int], message: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SpotifyService:
    """High-level Spotify helpers built on top of Spotipy."""

    def __init__(self, client: spotipy.Spotify) -> None:
        self.client = client
        self._current_user: Optional[dict] = None
        self._playlists_cache: Optional[List[dict]] = None
        self._shared_playlist_cache: Optional[dict] = None

    def set_shared_playlist_cache(self, cache: Optional[dict]) -> None:
        """Inject shared playlist cache produced by playlist_cache sync."""

        self._shared_playlist_cache = cache

    def _execute(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SpotifyException as exc:
            if exc.http_status == 429:
                retry_after: Optional[int] = None
                if exc.headers:
                    retry_after_header = exc.headers.get("Retry-After")
                    try:
                        retry_after = int(str(retry_after_header)) if retry_after_header is not None else None
                    except (TypeError, ValueError):
                        retry_after = None
                raise SpotifyRateLimitError(retry_after, exc.msg) from exc
            raise

    # ------------------------------------------------------------------
    # User & playlist discovery utilities
    # ------------------------------------------------------------------
    @property
    def current_user(self) -> dict:
        if self._current_user is None:
            self._current_user = self._execute(self.client.current_user)
        return self._current_user

    @property
    def user_id(self) -> str:
        return self.current_user["id"]

    def _fetch_all_playlists(self) -> List[dict]:
        playlists: List[dict] = []
        results = self._execute(self.client.current_user_playlists, limit=50)
        while results:
            playlists.extend(results["items"])
            if results["next"]:
                results = self._execute(self.client.next, results)
            else:
                break
        return playlists

    def ensure_playlist(self, name: str, public: bool = False, description: Optional[str] = None) -> dict:
        playlist = self.find_playlist_by_name(name)
        if playlist:
            return playlist
        playlist = self._execute(
            self.client.user_playlist_create,
            user=self.user_id,
            name=name,
            public=public,
            description=description or "",
        )
        # refresh cache
        self._playlists_cache = None
        return playlist

    def find_playlist_by_name(self, name: str) -> Optional[dict]:
        name_lower = name.strip().lower()
        if self._shared_playlist_cache:
            entry = self._shared_playlist_cache.get("by_name", {}).get(name_lower)
            if entry:
                return {
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "uri": entry.get("uri"),
                }
        if self._playlists_cache is None:
            self._playlists_cache = self._fetch_all_playlists()
        for playlist in self._playlists_cache:
            if playlist["name"].strip().lower() == name_lower:
                return playlist
        return None

    def list_all_playlists(self) -> List[dict]:
        """Return a fresh list of all user playlists."""

        return self._fetch_all_playlists()

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
        results = self._execute(self.client.current_user_saved_tracks, limit=page_limit)

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
            results = self._execute(self.client.next, results)

        normalized_direction = (direction or "oldest").lower()
        if normalized_direction not in {"oldest", "newest"}:
            normalized_direction = "oldest"

        if normalized_direction == "oldest":
            collected = list(reversed(collected))

        return [track_id for track_id, _ in collected]

    def get_playlist_tracks(self, playlist_id: str) -> List[str]:
        track_ids: List[str] = []
        results = self._execute(self.client.playlist_items, playlist_id, fields="items(track(id)),next")
        while results:
            track_ids.extend(
                track["track"]["id"]
                for track in results["items"]
                if track.get("track") and track["track"].get("id")
            )
            if results["next"]:
                results = self._execute(self.client.next, results)
            else:
                break
        return track_ids

    def get_playlist_items_with_added_at(self, playlist_id: str) -> List[Dict[str, Any]]:
        """Return playlist entries including track metadata and added timestamps."""

        items: List[Dict[str, Any]] = []
        results = self._execute(
            self.client.playlist_items,
            playlist_id,
            fields="items(added_at,track(id,name,artists(name))),next",
        )

        while results:
            for entry in results.get("items", []):
                track = entry.get("track")
                if not track or not track.get("id"):
                    continue
                artist_names = []
                if track.get("artists"):
                    artist_names = [artist.get("name", "") for artist in track["artists"]]
                items.append(
                    {
                        "id": track["id"],
                        "name": track.get("name", ""),
                        "artists": ", ".join(filter(None, artist_names)),
                        "added_at": entry.get("added_at"),
                    }
                )
            if results.get("next"):
                results = self._execute(self.client.next, results)
            else:
                break

        return items

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
            self._execute(self.client.playlist_add_items, playlist_id, batch)
            total += len(batch)
        return total

    def remove_tracks(self, playlist_id: str, track_ids: Iterable[str]) -> int:
        track_list = list(track_ids)
        if not track_list:
            return 0
        total = 0
        for offset in range(0, len(track_list), BATCH_SIZE):
            batch = track_list[offset : offset + BATCH_SIZE]
            self._execute(
                self.client.playlist_remove_all_occurrences_of_items, playlist_id, batch
            )
            total += len(batch)
        return total

    def replace_tracks(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        track_list = list(track_ids)
        if not track_list:
            self._execute(self.client.playlist_replace_items, playlist_id, [])
            return

        first_batch = track_list[:100]
        self._execute(self.client.playlist_replace_items, playlist_id, first_batch)
        remaining = track_list[100:]
        for offset in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[offset : offset + BATCH_SIZE]
            self._execute(self.client.playlist_add_items, playlist_id, batch)

    def search_track(self, name: str, artist: Optional[str] = None, limit: int = 5) -> Optional[str]:
        query = f"track:{name}"
        if artist:
            query += f" artist:{artist}"
        results = self.client.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if items:
            return items[0]["id"]

        relaxed_query = f"{name} {artist}" if artist else name
        results = self.client.search(q=relaxed_query, type="track", limit=limit)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return None

        needle_name = (name or "").lower()
        needle_artist = (artist or "").lower()
        for item in items:
            item_name = item.get("name", "").lower()
            item_artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
            item_artists_lower = item_artists.lower()
            if needle_name and needle_name in item_name:
                if not needle_artist or needle_artist in item_artists_lower:
                    return item.get("id")
        return items[0].get("id") if items else None

    def update_playlist_details(
        self,
        playlist_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        public: Optional[bool] = None,
    ) -> None:
        payload = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if public is not None:
            payload["public"] = public
        if payload:
            self.client.playlist_change_details(playlist_id, **payload)

    def upload_playlist_cover(self, playlist_id: str, image_b64: str) -> None:
        self.client.playlist_upload_cover_image(playlist_id, image_b64)

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
