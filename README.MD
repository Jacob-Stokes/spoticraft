# Spotifreak

Work-in-progress rewrite of a modular Spotify automation toolkit. The goal is to provide a single CLI and supervisor for managing long-running sync jobs (playlist mirroring, Last.fm integrations, cover rotations, etc.).

## Getting Started
- Run `pip install -e .` (inside a virtualenv) to install dependencies such as Spotipy.
- Run `spotifreak init` (once implemented) to configure API credentials.
- Use `spotifreak serve` to launch the supervisor and keep syncs running in the background.
- Manage sync definitions with `spotifreak create`, `spotifreak list`, `spotifreak update`, and `spotifreak run`.

> Planning notes live in `PLAN.MD` (ignored by Git) while the design is still evolving.

## Current Status
- Architecture and configuration schema drafted (see `PLAN.MD`).
- Supervisor now schedules syncs via APScheduler and records run history.
- Playlist mirror module supports configurable saved-track scan strategies (`max_tracks`, `lookback_count`, `lookback_days`, `full_scan`, and `scan_direction`).
- Supervisor exposes IPC for lifecycle commands (`start`, `pause`, `resume`, `delete`) and hot-reloads config changes.
- Playlist retention module trims primary playlists by age/count and optionally archives removals.
- Last.fm top tracks module replaces a playlist with your current favourites when rankings change.
- Playlist presentation module rotates cover art, titles, and descriptions on interval or sunrise/sunset phases.

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

### Playlist Presentation Options

```yaml
type: playlist_presentation
options:
  playlist:
    kind: playlist_name
    name: Presentation Demo
  phases:
    mode: sunrise_sunset
    sunrise:
      latitude: 51.5074
      longitude: -0.1278
  cover:
    enabled: true
    selection: sequential
    assets:
      morning: [assets/morning.jpg]
      day: [assets/day.jpg]
      evening: [assets/evening.jpg]
      night: [assets/night.jpg]
  title:
    enabled: true
    selection: random
    assets:
      default:
        - Morning Flow
        - Afternoon Pulse
        - Sunset Hues
        - Midnight Echoes
  description:
    enabled: true
    use_dynamic: true
    selection: sequential
    dynamic_templates:
      - Refreshed at {time} on {weekday}
      - Curated vibes for {weekday}
```

- Supply images sized for Spotify (640×640, <256KB) at the referenced paths.
- Titles and descriptions rotate per phase; placeholders `{time}`, `{weekday}`, `{date}` are substituted when `use_dynamic` is enabled.
- If `interval_seconds` is omitted, the module uses the sync’s `schedule.interval` (default throttled to five minutes) to avoid rate-limiting.

### Running the Supervisor Persistently

```
spotifreak serve --config-dir ~/.spotifreak --log-file ~/spotifreak.log
```

- `spotifreak status` displays supervisor job state; `start|pause|resume|delete <id>` control individual syncs via IPC.
- `spotifreak logs <id>` inspects the JSON run history for a given sync.
- For long-lived deployments wrap the serve command in your process manager of choice (systemd, launchd, pm2). A sample systemd unit:

```
[Unit]
Description=Spotifreak Supervisor
After=network.target

[Service]
ExecStart=/usr/bin/env spotifreak serve --config-dir /home/user/.spotifreak --log-file /var/log/spotifreak.log
Restart=always

[Install]
WantedBy=multi-user.target
```

### Docker Deployment

Build the container image and run the supervisor in the background:

```
docker build -t spotifreak .
docker run -d \
  --name spotifreak-supervisor \
  --restart unless-stopped \
  -v ~/.spotifreak:/config \
  -v ~/.spotifreak/state:/state \
  -v ~/.spotifreak/logs:/logs \
  spotifreak \
  serve --config-dir /config --log-file /logs/spotifreak.log
```

Manage the running supervisor via Docker:

```
docker exec -it spotifreak-supervisor spotifreak status --config-dir /config
docker exec -it spotifreak-supervisor spotifreak start liked-to-archive --config-dir /config
```

`docker-compose.example.yml` demonstrates running the supervisor and web API side-by-side with shared volumes.

### Web API (Experimental)

Install the optional dependencies and launch the API:

```
pip install '.[web]'
uvicorn spotifreak.web.api:app --host 0.0.0.0 --port 8080
```

Set `SPOTIFREAK_CONFIG_DIR` to point at your configuration directory (defaults to `~/.spotifreak`). Available endpoints include `GET /status`, `GET /syncs`, `POST /syncs/{id}/start|pause|resume|delete`, and `GET /syncs/{id}/history?tail=10`. Keep the supervisor running alongside the API (e.g., via Docker Compose) so IPC commands succeed.
