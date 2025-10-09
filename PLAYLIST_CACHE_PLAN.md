# Playlist Cache Sync Design Sketch

## Goals
- Provide a single, user-visible sync responsible for enumerating Spotify playlists and storing a shared cache.
- Let other syncs (playlist_mirror, lastfm_top_tracks, future modules) consume the cache instead of making their own `current_user_playlists` calls.
- Avoid hidden Spotifreak-specific TTLs; cadence is entirely controlled by the cache sync schedule.

## New Sync Type: `playlist_cache`
```yaml
id: playlist-cache
type: playlist_cache
schedule:
  interval: 30m
state_file: playlist-cache.json  # optional override
options:
  include_public: true
  include_private: true
  include_collaborative: true
  owners: ["self"]       # allow filtering by owner ids if needed later
```

### Behaviour
1. Authenticate via the usual Spotify client factory.
2. Fetch all playlists (`current_user_playlists` paging until exhaustion).
3. Normalise each playlist into a compact record:
   ```json
   {
     "id": "7rSbbAjhKodB9nBkdu3JL4",
     "name": "Oct25",
     "owner": "spotify_user_id",
     "collaborative": false,
     "public": true,
     "snapshot_id": "..."
   }
   ```
4. Persist the list in the sync state file as:
   ```json
   {
     "last_refreshed": "2025-10-09T14:20:00Z",
     "playlists": {
       "oct25": { ... },
       "(2025)": { ... },
       "all liked songs": { ... },
       "spotify:playlist:7rS...": { ... }
     }
   }
   ```
   - Keys include both lowercased names and stable URIs.
   - Keep optional metadata for fast lookups (snapshot, owner, etc.).

5. Update run history (as with other syncs).

## Consuming the Cache
### SpotifyService helpers
- `load_cached_playlist(name_or_uri, cache_store)` – return playlist record if the cache is present and fresh.
- `resolve_playlist(...)` – use cache first; if missed, fall back to fetch and optionally report a cache miss metric.

### Sync Flow
1. Syncs receive `context.shared_cache`, a dict loaded lazily at supervisor startup from `playlist-cache.json` (if it exists).
2. If the cache is stale (based on `last_refreshed` vs. user-defined acceptable age, e.g. 2× cache sync interval), they can either:
   - trigger a local fallback fetch, or
   - log a warning encouraging the user to run the cache sync more frequently.

3. When a sync falls back to a direct fetch, it can update the shared cache copy to keep things hot until the next cache sync pass.

## Supervisor Integration
- At startup, load `playlist-cache.json` into a shared map keyed by sync ID (or a global slot) and pass to `SyncContext` as `shared_cache`.
- When the playlist cache sync runs, it writes the updated JSON and signals the supervisor to refresh the in-memory copy (e.g., via file watch or in-process notification).

## Error Handling
- If the cache sync fails (Spotify 429, etc.), leave the previous data intact but log the failure.
- Downstream syncs should tolerate missing cache entries gracefully.

## Next Steps
1. Implement the `playlist_cache` sync skeleton and shared state structure.
2. Expose helper methods in `SpotifyService` for cache-aware playlist lookup.
3. Update existing modules to use the shared cache if available.
4. Ship a template (`playlist_cache_basic.yml`) and documentation emphasising the benefits (lower API usage for `playlist_mirror`, `lastfm_top_tracks`, etc.).
5. Optionally add supervisor metrics/logging for cache age to help users tune the schedule.
