# Spoticraft

Work-in-progress rewrite of a modular Spotify automation toolkit. The goal is to provide a single CLI and supervisor for managing long-running sync jobs (playlist mirroring, Last.fm integrations, cover rotations, etc.).

## Getting Started
- Run `pip install -e .` (inside a virtualenv) to install dependencies such as Spotipy.
- Run `spoticraft init` (once implemented) to configure API credentials.
- Use `spoticraft serve` to launch the supervisor and keep syncs running in the background.
- Manage sync definitions with `spoticraft create`, `spoticraft list`, `spoticraft update`, and `spoticraft run`.

> Planning notes live in `PLAN.MD` (ignored by Git) while the design is still evolving.

## Current Status
- Architecture and configuration schema drafted (see `PLAN.MD`).
- Supervisor now schedules syncs via APScheduler and records run history.
- Playlist mirror module supports configurable saved-track scan strategies (`max_tracks`, `lookback_count`, `lookback_days`, `full_scan`, and `scan_direction`).

### Saved Tracks Scan Options
When using the `saved_tracks` source, add any of the following to the source block:

```yaml
options:
  source:
    kind: saved_tracks
    max_tracks: 100            # Only inspect the newest 100 saved tracks
    lookback_count: 50         # Alternative: cap to the latest 50 entries regardless of max_tracks
    lookback_days: 7           # Ignore anything added more than 7 days ago
    full_scan: false           # Stop fetching once the last processed track is seen
    scan_direction: newest     # Process newest-first instead of oldest-first
```

Combine these to balance API usage vs. coverage per sync definition.
