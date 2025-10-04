"""Command-line entry point for Spotifreak."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

import typer
from spotipy.exceptions import SpotifyException

from .config import ConfigError, ConfigPaths, bootstrap
from .app_context import determine_paths, load_context
from .modules import SyncContext, default_registry
from .supervisor import Supervisor
from .auth import SpotifyClientFactory
from .services import SpotifyService
from .state import load_state, state_path_for_sync
from .logging import configure_logging, get_logger
from .ipc import send_ipc_command, IPCError
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Spotifreak supervisor and CLI controller.")
state_app = typer.Typer(help="Inspect and modify sync state.")
app.add_typer(state_app, name="state")
console = Console()


def _determine_default_log_level(config_dir: Optional[Path]) -> str:
    if config_dir is None:
        paths = ConfigPaths.default()
    else:
        paths = ConfigPaths.from_base_dir(config_dir)

    config_path = paths.global_config
    if not config_path.exists():
        return "INFO"

    try:
        import yaml  # local import to avoid hard dependency before install

        with config_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        runtime = payload.get("runtime", {})
        log_level = runtime.get("log_level")
        if isinstance(log_level, str) and log_level.strip():
            return log_level.upper()
    except Exception:
        return "INFO"

    return "INFO"


def _bootstrap_logging(
    ctx: typer.Context,
    verbose: bool,
    json_logs: bool,
    log_file: Optional[Path],
    config_dir: Optional[Path],
) -> None:
    """Initialise logging once per CLI invocation."""

    if ctx.obj is None:
        ctx.obj = {}

    if ctx.obj.get("_logging_configured"):
        return

    level = "DEBUG" if verbose else _determine_default_log_level(config_dir)
    configure_logging(level=level, json_output=json_logs, log_file=log_file)
    ctx.obj["logger"] = get_logger("spotifreak.cli")
    ctx.obj["log_level"] = level
    ctx.obj["json_logs"] = json_logs
    ctx.obj["log_file_path"] = log_file
    ctx.obj["force_log_level"] = verbose
    ctx.obj["_logging_configured"] = True


@app.callback(invoke_without_command=True)
def cli(  # noqa: D401 - Typer generates help text.
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
    json_logs: bool = typer.Option(False, "--json-logs", help="Emit JSON-formatted logs."),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        dir_okay=True,
        file_okay=True,
        writable=True,
        resolve_path=True,
        help="Optional file to append structured logs to.",
    ),
) -> None:
    """Spotifreak command group."""

    _bootstrap_logging(ctx, verbose, json_logs, log_file, None)

    if ctx.invoked_subcommand is None:
        typer.echo(app.get_help(ctx))


def _logger(ctx: typer.Context):
    return ctx.obj.get("logger", get_logger("spotifreak.cli"))


def _maybe_update_log_level(ctx: typer.Context, config_dir: Optional[Path]) -> None:
    if ctx.obj.get("force_log_level"):
        return

    desired = _determine_default_log_level(config_dir)
    current = ctx.obj.get("log_level")
    if desired != current:
        configure_logging(
            level=desired,
            json_output=ctx.obj.get("json_logs", False),
            log_file=ctx.obj.get("log_file_path"),
        )
        ctx.obj["logger"] = get_logger("spotifreak.cli")
        ctx.obj["log_level"] = desired


def parse_track_id(value: str) -> str:
    value = value.strip()
    if value.startswith("https://open.spotify.com/track/"):
        value = value.split("track/")[1]
        value = value.split("?")[0]
    elif value.startswith("spotify:track:"):
        value = value.split("spotify:track:")[1]
    if not value or len(value) != 22:
        raise typer.BadParameter("Provide a valid Spotify track URL or ID (22 characters).")
    return value
def init(
    ctx: typer.Context,
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        writable=True,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config.yml"),
) -> None:
    """Initial setup flow for global configuration."""

    log = _logger(ctx)

    try:
        paths = ConfigPaths.from_base_dir(config_dir) if config_dir else ConfigPaths.default()
        report = bootstrap(paths, overwrite=force)
    except ConfigError as exc:
        log.error("init.failed", error=str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Configuration directory: {paths.base_dir}")
    typer.echo(f"Sync definitions directory: {paths.syncs_dir}")

    if report.global_config_created:
        if report.global_config_overwritten:
            typer.echo(f"Global config overwritten at: {paths.global_config}")
        else:
            typer.echo(f"Global config created at: {paths.global_config}")
            typer.echo("Update Spotify credentials before running syncs.")
    else:
        typer.echo(f"Global config already exists at: {paths.global_config}")
        typer.echo("Use --force to regenerate with default values.")

    log.info(
        "init.completed",
        base_dir=str(paths.base_dir),
        syncs_dir=str(paths.syncs_dir),
        state_dir=str(paths.state_dir),
        global_config=str(paths.global_config),
        force=force,
        base_created=report.base_created,
        syncs_dir_created=report.syncs_dir_created,
        state_dir_created=report.state_dir_created,
        global_config_created=report.global_config_created,
        global_config_overwritten=report.global_config_overwritten,
    )


@app.command()
def serve(
    ctx: typer.Context,
    reload: bool = typer.Option(True, help="Watch config directory for changes."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Start the background supervisor."""

    log = _logger(ctx)

    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("serve.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    supervisor = Supervisor(
        config=context.global_config,
        paths=context.paths,
        syncs=context.syncs,
        logger=log,
    )
    supervisor.run(hot_reload=reload)


@app.command()
def create(
    ctx: typer.Context,
    sync_type: str = typer.Argument(..., help="Sync module type to instantiate."),
    name: str = typer.Option(None, "--name", help="Optional sync identifier. Defaults to module name."),
) -> None:
    """Create a new sync definition from a module template."""

    log = _logger(ctx)
    log.info(
        "create.not_implemented",
        message="Sync creation not implemented yet",
        sync_type=sync_type,
        name=name,
    )


@app.command("list")
def list_syncs(
    ctx: typer.Context,
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """List configured syncs and their status."""

    log = _logger(ctx)

    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("list.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    if not context.syncs:
        console.print("[yellow]No syncs configured yet.[/yellow]")
        console.print(f"Add YAML files to {context.paths.syncs_dir} to register syncs.")
        log.info("list.completed", sync_count=0)
        return

    table = Table(title="Configured Spotifreak Syncs")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Type")
    table.add_column("Schedule")
    table.add_column("State File")

    for sync in context.syncs:
        schedule = sync.schedule.interval or sync.schedule.cron or "-"

        state_path = state_path_for_sync(context.paths, context.global_config, sync)
        table.add_row(sync.id, sync.type, schedule, str(state_path))

    console.print(table)
    log.info("list.completed", sync_count=len(context.syncs))


@app.command()
def status(
    ctx: typer.Context,
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Show supervisor job status."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("status.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    socket_path = Path(context.global_config.supervisor.ipc_socket).expanduser()
    try:
        response = send_ipc_command(socket_path, {"command": "status"})
    except IPCError as exc:
        log.error("status.ipc_failed", error=str(exc))
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if response.get("status") != "ok":
        typer.echo(f"Supervisor error: {response.get('message')}")
        raise typer.Exit(code=1)

    jobs = response.get("jobs", [])
    if not jobs:
        typer.echo("No scheduled jobs.")
        return

    table = Table(title="Supervisor Jobs")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Next Run")
    table.add_column("Paused")
    table.add_column("Missed")
    for job in jobs:
        table.add_row(
            str(job.get("id")),
            job.get("next_run") or "-",
            str(job.get("paused")),
            str(job.get("missed")),
        )
    console.print(table)


@app.command()
def update(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Existing sync identifier."),
) -> None:
    """Update parameters for an existing sync."""

    log = _logger(ctx)
    log.info(
        "update.not_implemented",
        message="Sync update not implemented yet",
        sync_id=sync_id,
    )


@app.command()
def start(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to start."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Start a sync managed by the supervisor."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("start.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    if sync_id not in {sync.id for sync in context.syncs}:
        typer.echo(f"Sync '{sync_id}' not found in {context.paths.syncs_dir}")
        raise typer.Exit(code=1)

    socket_path = Path(context.global_config.supervisor.ipc_socket).expanduser()
    try:
        response = send_ipc_command(socket_path, {"command": "start", "sync_id": sync_id})
    except IPCError as exc:
        log.error("start.ipc_failed", error=str(exc))
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if response.get("status") != "ok":
        typer.echo(f"Supervisor error: {response.get('message')}")
        raise typer.Exit(code=1)

    typer.echo(response.get("message", "Triggered"))


@app.command()
def pause(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to pause."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Pause a running sync."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("pause.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    if sync_id not in {sync.id for sync in context.syncs}:
        typer.echo(f"Sync '{sync_id}' not found in {context.paths.syncs_dir}")
        raise typer.Exit(code=1)

    socket_path = Path(context.global_config.supervisor.ipc_socket).expanduser()
    try:
        response = send_ipc_command(socket_path, {"command": "pause", "sync_id": sync_id})
    except IPCError as exc:
        log.error("pause.ipc_failed", error=str(exc))
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if response.get("status") != "ok":
        typer.echo(f"Supervisor error: {response.get('message')}")
        raise typer.Exit(code=1)

    typer.echo(response.get("message", "Paused"))


@app.command()
def resume(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to resume."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Resume a paused sync."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("resume.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    if sync_id not in {sync.id for sync in context.syncs}:
        typer.echo(f"Sync '{sync_id}' not found in {context.paths.syncs_dir}")
        raise typer.Exit(code=1)

    socket_path = Path(context.global_config.supervisor.ipc_socket).expanduser()
    try:
        response = send_ipc_command(socket_path, {"command": "resume", "sync_id": sync_id})
    except IPCError as exc:
        log.error("resume.ipc_failed", error=str(exc))
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if response.get("status") != "ok":
        typer.echo(f"Supervisor error: {response.get('message')}")
        raise typer.Exit(code=1)

    typer.echo(response.get("message", "Resumed"))


@app.command()
def delete(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to delete."),
    force: bool = typer.Option(False, "--force", help="Delete without supervisor confirmation."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Delete a sync definition."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("delete.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    if sync_id not in {sync.id for sync in context.syncs}:
        typer.echo(f"Sync '{sync_id}' not found in {context.paths.syncs_dir}")
        raise typer.Exit(code=1)

    socket_path = Path(context.global_config.supervisor.ipc_socket).expanduser()
    try:
        response = send_ipc_command(socket_path, {"command": "delete", "sync_id": sync_id, "force": force})
    except IPCError as exc:
        log.error("delete.ipc_failed", error=str(exc))
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    if response.get("status") != "ok":
        typer.echo(f"Supervisor error: {response.get('message')}")
        raise typer.Exit(code=1)

    typer.echo(response.get("message", "Removed"))


@app.command()
def run(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to execute once."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Execute a sync once outside the supervisor."""

    log = _logger(ctx)

    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("run.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        sync_config = next(sync for sync in context.syncs if sync.id == sync_id)
    except StopIteration:
        typer.echo(f"Sync '{sync_id}' not found in {paths.syncs_dir}")
        log.error("run.missing_sync", sync_id=sync_id)
        raise typer.Exit(code=1)

    state_path = state_path_for_sync(context.paths, context.global_config, sync_config)
    sync_state = load_state(state_path)

    run_started = datetime.now(timezone.utc)
    run_id = run_started.isoformat()
    sync_state.begin_run(run_id, started_at=run_id)

    try:
        module_factory = default_registry.get(sync_config.type)
    except KeyError as exc:
        typer.echo(str(exc))
        log.error("run.missing_module", sync_type=sync_config.type)
        sync_state.complete_run(
            run_id,
            status="failed",
            error=str(exc),
            details={"mode": "cli", "stage": "module_lookup"},
        )
        sync_state.save()
        raise typer.Exit(code=1) from exc

    module_logger = log.bind(sync_id=sync_id, sync_type=sync_config.type)

    spotify_service: Optional[SpotifyService] = None
    try:
        spotify_factory = SpotifyClientFactory(context.global_config)
        spotify_client = spotify_factory.get_client()
        spotify_service = SpotifyService(spotify_client)
    except Exception as exc:  # pragma: no cover - depends on runtime creds
        module_logger.error("run.spotify_init_failed", error=str(exc))
        sync_state.complete_run(
            run_id,
            status="failed",
            error=str(exc),
            details={"mode": "cli", "stage": "spotify_init"},
        )
        sync_state.save()
        typer.echo(f"Spotify setup failed: {exc}")
        raise typer.Exit(code=1) from exc

    module = module_factory(sync_config)
    sync_context = SyncContext(
        logger=module_logger,
        spotify=spotify_service,
        state=sync_state,
        global_config=context.global_config,
        paths=context.paths,
    )

    def _build_details(stage: Optional[str] = None) -> dict:
        details = {"mode": "cli"}
        if stage:
            details["stage"] = stage
        summary = getattr(module, "last_run_summary", None)
        if isinstance(summary, dict):
            details.update(summary)
        return details

    try:
        module_logger.info("run.start")
        module.run(sync_context)
    except Exception as exc:  # pragma: no cover - depends on module implementation
        module_logger.exception("run.failed", error=str(exc))
        sync_state.complete_run(
            run_id,
            status="failed",
            error=str(exc),
            details=_build_details(stage="module_execution"),
        )
        sync_state.save()
        raise typer.Exit(code=1) from exc
    else:
        module_logger.info("run.completed")
        sync_state.complete_run(
            run_id,
            status="success",
            details=_build_details(),
        )
    finally:
        sync_state.save()


@state_app.command("set-last-track")
def state_set_last_track(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Existing sync identifier."),
    track: str = typer.Argument(..., help="Spotify track URL or ID to set as the baseline."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Set the last processed track for a sync."""

    log = _logger(ctx)
    track_id = parse_track_id(track)

    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("state.set_last_track.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        sync_config = next(sync for sync in context.syncs if sync.id == sync_id)
    except StopIteration:
        typer.echo(f"Sync '{sync_id}' not found in {paths.syncs_dir}")
        log.error("state.set_last_track.missing_sync", sync_id=sync_id)
        raise typer.Exit(code=1)

    state_path = state_path_for_sync(context.paths, context.global_config, sync_config)
    sync_state = load_state(state_path)
    sync_state.set_last_processed_track_id(track_id)
    sync_state.save()

    log.info(
        "state.set_last_track.completed",
        sync_id=sync_id,
        track_id=track_id,
        state_path=str(state_path),
    )
    typer.echo(f"Last processed track for '{sync_id}' set to {track_id} (saved to {state_path})")


@app.command()
def doctor(
    ctx: typer.Context,
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Run basic diagnostics against the Spotify API."""

    log = _logger(ctx)

    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("doctor.config_failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        spotify_factory = SpotifyClientFactory(context.global_config)
        spotify_client = spotify_factory.get_client()
        spotify_service = SpotifyService(spotify_client)
    except Exception as exc:  # pragma: no cover - depends on runtime creds
        log.error("doctor.spotify_init_failed", error=str(exc))
        typer.echo(f"Spotify setup failed: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        spotify_client.current_user_playlists(limit=1)
        current_user = spotify_service.current_user
    except SpotifyException as exc:
        headers = getattr(exc, "headers", None) or {}
        retry_after = None
        if isinstance(headers, dict):
            retry_after = headers.get("Retry-After") or headers.get("retry-after")

        if exc.http_status == 429:
            log.warning(
                "doctor.rate_limited",
                retry_after=retry_after,
                message="Spotify API returned HTTP 429.",
            )
            message = "Rate limited by Spotify (HTTP 429)."
            if retry_after:
                message += f" Retry after {retry_after} seconds."
            typer.echo(message)
            raise typer.Exit(code=2) from exc

        log.error(
            "doctor.spotify_api_error",
            status=exc.http_status,
            error=str(exc),
        )
        typer.echo(f"Spotify API error ({exc.http_status}): {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # pragma: no cover - network/runtime issues
        log.error("doctor.spotify_call_failed", error=str(exc))
        typer.echo(f"Spotify API call failed: {exc}")
        raise typer.Exit(code=1) from exc

    display_name = current_user.get("display_name") or current_user.get("id")
    user_id = current_user.get("id")

    log.info(
        "doctor.completed",
        user_id=user_id,
        display_name=display_name,
    )
    typer.echo("Spotify API access OK.")
    if display_name:
        typer.echo(f"Authenticated as: {display_name} ({user_id})")


@app.command()
def logs(
    ctx: typer.Context,
    sync_id: str = typer.Argument(..., help="Sync identifier to inspect."),
    tail: int = typer.Option(10, help="Number of recent runs to display."),
    config_dir: Optional[Path] = typer.Option(
        None,
        "--config-dir",
        dir_okay=True,
        file_okay=False,
        resolve_path=True,
        help="Base directory for config files (defaults to ~/.spotifreak).",
    ),
) -> None:
    """Show recent logs for a sync."""

    log = _logger(ctx)
    _maybe_update_log_level(ctx, config_dir)

    try:
        paths = determine_paths(config_dir)
        context = load_context(paths)
    except ConfigError as exc:
        log.error("logs.failed", error=str(exc))
        typer.echo(f"Error loading configuration: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        sync_config = next(sync for sync in context.syncs if sync.id == sync_id)
    except StopIteration:
        typer.echo(f"Sync '{sync_id}' not found in {paths.syncs_dir}")
        log.error("logs.missing_sync", sync_id=sync_id)
        raise typer.Exit(code=1)

    state_path = state_path_for_sync(context.paths, context.global_config, sync_config)
    state = load_state(state_path)
    history = state.data.get("run_history", [])
    if not history:
        typer.echo("No run history available yet.")
        return

    entries = history[-tail:]
    for item in entries:
        started = item.get("started_at", "?")
        status = item.get("status", "?")
        message = item.get("details", {}).get("reason") or item.get("details", {}).get("status", "")
        typer.echo(f"[{status}] {started} -> {item.get('completed_at', '-')} {message}")


def main() -> None:
    """Run the Typer application."""

    app()


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    main()
