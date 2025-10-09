"""Supervisor runtime scaffolding for Spotifreak."""

from __future__ import annotations

import json
import re
import signal
import socket
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from watchfiles import watch

from .auth import SpotifyClientFactory
from .config import ConfigPaths, GlobalConfig, SyncConfig, load_global_config, load_sync_configs
from .modules import ModuleRegistry, SyncContext, default_registry
from .services import SpotifyService
from .state import load_state, state_path_for_sync


@dataclass
class Supervisor:
    """Supervisor process responsible for scheduling syncs."""

    config: GlobalConfig
    paths: ConfigPaths
    syncs: List[SyncConfig]
    logger: structlog.stdlib.BoundLogger
    registry: ModuleRegistry = default_registry
    _timezone: ZoneInfo = field(init=False, repr=False)
    _timezone_source: str = field(init=False, repr=False)
    _scheduler: BackgroundScheduler = field(init=False, repr=False)
    _stop_event: threading.Event = field(init=False, repr=False)
    _hot_reload_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _ipc_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _jobs_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _ipc_socket: Optional[Path] = field(default=None, init=False, repr=False)
    _sync_index: Dict[str, SyncConfig] = field(default_factory=dict, init=False, repr=False)
    _playlist_cache_syncs: List[SyncConfig] = field(default_factory=list, init=False, repr=False)
    _shared_playlist_cache: Optional[dict] = field(default=None, init=False, repr=False)
    _playlist_cache_mtimes: Dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._timezone, self._timezone_source = self._resolve_timezone(self.config.runtime.timezone)
        self._stop_event = threading.Event()
        self._scheduler = self._create_scheduler()
        self._sync_index = {sync.id: sync for sync in self.syncs}
        self._ipc_socket = Path(self.config.supervisor.ipc_socket).expanduser()
        self._playlist_cache_syncs = [sync for sync in self.syncs if sync.type == "playlist_cache"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, hot_reload: bool = True) -> None:
        """Start the supervisor event loop and block until interrupted."""

        sync_count = len(self.syncs)
        self.logger.info(
            "supervisor.start",
            sync_count=sync_count,
            hot_reload=hot_reload,
            timezone=self._timezone_source,
        )

        if self._timezone_source != self.config.runtime.timezone:
            self.logger.warning(
                "supervisor.timezone_fallback",
                configured=self.config.runtime.timezone,
                using=self._timezone_source,
            )

        if hot_reload:
            self.logger.info(
                "supervisor.hot_reload_enabled",
                watched_dirs=[str(self.paths.syncs_dir), str(self.paths.global_config)],
            )

        if sync_count == 0:
            self.logger.warning(
                "supervisor.no_syncs",
                message="No syncs defined. Supervisor will idle until exit.",
            )

        self._register_all_syncs()

        self.logger.info(
            "supervisor.scheduler_starting",
            jobs=len(self._scheduler.get_jobs()),
        )

        self._scheduler.start()
        self._install_signal_handlers()

        if hot_reload:
            self._start_hot_reload_watcher()

        self._start_ipc_server()

        try:
            while not self._stop_event.wait(timeout=1):
                pass
        except KeyboardInterrupt:
            self.logger.info("supervisor.stop", reason="keyboard_interrupt")
        finally:
            self.shutdown()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_timezone(tz_name: str) -> tuple[ZoneInfo, str]:
        try:
            tz = ZoneInfo(tz_name)
            label = tz.key if hasattr(tz, "key") else str(tz)
            return tz, label
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
            return tz, "UTC"
        except Exception:  # pragma: no cover - defensive fallback
            tz = ZoneInfo("UTC")
            return tz, "UTC"

    def _create_scheduler(self) -> BlockingScheduler:
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        return BackgroundScheduler(timezone=self._timezone, executors=executors)

    def _build_trigger(self, sync: SyncConfig):
        schedule = sync.schedule
        if schedule.interval:
            return self._interval_trigger(schedule.interval)
        if schedule.cron:
            return CronTrigger.from_crontab(schedule.cron, timezone=self._timezone)
        raise ValueError("Sync schedule must define interval or cron expression.")

    def _interval_trigger(self, expression: str) -> IntervalTrigger:
        total_seconds = self._parse_interval(expression)
        return IntervalTrigger(seconds=total_seconds, timezone=self._timezone)

    @staticmethod
    def _parse_interval(expression: str) -> int:
        pattern = re.compile(r"(\d+)([smhd])", re.IGNORECASE)
        total = 0
        pos = 0
        for match in pattern.finditer(expression.strip()):
            if match.start() != pos:
                raise ValueError(f"Invalid interval expression: {expression}")
            value = int(match.group(1))
            unit = match.group(2).lower()
            if unit == "s":
                total += value
            elif unit == "m":
                total += value * 60
            elif unit == "h":
                total += value * 3600
            elif unit == "d":
                total += value * 86400
            else:  # pragma: no cover - unreachable due to regex
                raise ValueError(f"Unsupported interval unit: {unit}")
            pos = match.end()

        if pos != len(expression.strip()) or total <= 0:
            raise ValueError(f"Invalid interval expression: {expression}")
        return total

    def _register_all_syncs(self) -> None:
        with self._jobs_lock:
            for sync in self.syncs:
                self._register_sync_job(sync)

    def _register_sync_job(self, sync: SyncConfig, *, immediate: bool = False) -> None:
        module_logger = self.logger.bind(sync_id=sync.id, sync_type=sync.type)
        try:
            trigger = self._build_trigger(sync)
        except ValueError as exc:
            module_logger.error("supervisor.schedule_invalid", error=str(exc))
            return

        next_run = datetime.now(self._timezone) if immediate else None
        try:
            self._scheduler.add_job(
                self._run_sync,
                trigger=trigger,
                id=sync.id,
                name=f"{sync.type}:{sync.id}",
                args=[sync.id],
                coalesce=True,
                max_instances=1,
                replace_existing=True,
                next_run_time=next_run,
            )
        except Exception as exc:  # pragma: no cover - APScheduler edge conditions
            module_logger.exception("supervisor.job_registration_failed", error=str(exc))
            return

        module_logger.info("supervisor.sync_scheduled", schedule=str(trigger))

    def _run_sync(self, sync_id: str) -> None:
        sync = self._sync_index.get(sync_id)
        if not sync:
            self.logger.warning("supervisor.sync_missing", sync_id=sync_id)
            return
        module_logger = self.logger.bind(sync_id=sync.id, sync_type=sync.type)
        state_path = state_path_for_sync(self.paths, self.config, sync)
        sync_state = load_state(state_path)

        shared_cache = self._refresh_shared_playlist_cache()

        run_started = datetime.now(timezone.utc)
        run_id = run_started.isoformat()
        sync_state.begin_run(run_id, started_at=run_id)

        try:
            factory = self.registry.get(sync.type)
        except KeyError as exc:
            error_message = str(exc)
            module_logger.error(
                "supervisor.module_missing",
                message=error_message,
            )
            sync_state.complete_run(
                run_id,
                status="failed",
                error=error_message,
                details=self._run_details(mode="supervisor", stage="module_lookup"),
            )
            sync_state.save()
            return

        module = factory(sync)

        try:
            spotify_factory = SpotifyClientFactory(self.config)
            spotify_client = spotify_factory.get_client()
            spotify_service = SpotifyService(spotify_client)
            if shared_cache:
                spotify_service.set_shared_playlist_cache(shared_cache)
        except Exception as exc:  # pragma: no cover - depends on runtime creds
            error_message = str(exc)
            module_logger.error(
                "supervisor.spotify_init_failed",
                error=error_message,
            )
            sync_state.complete_run(
                run_id,
                status="failed",
                error=error_message,
                details=self._run_details(mode="supervisor", stage="spotify_init"),
            )
            sync_state.save()
            return

        context = SyncContext(
            logger=module_logger,
            spotify=spotify_service,
            state=sync_state,
            global_config=self.config,
            paths=self.paths,
            shared_cache=shared_cache,
        )

        details: Optional[Dict[str, object]] = None
        try:
            module_logger.info("supervisor.sync_run_start")
            module.run(context)
        except Exception as exc:  # pragma: no cover - module runtime behaviour
            error_message = str(exc)
            module_logger.exception(
                "supervisor.sync_failed",
                error=error_message,
            )
            summary = getattr(module, "last_run_summary", None)
            details = self._run_details(
                mode="supervisor",
                stage="module_execution",
                summary=summary,
            )
            sync_state.complete_run(
                run_id,
                status="failed",
                error=error_message,
                details=details,
            )
        else:
            module_logger.info("supervisor.sync_completed")
            summary = getattr(module, "last_run_summary", None)
            details = self._run_details(mode="supervisor", summary=summary)
            sync_state.complete_run(
                run_id,
                status="success",
                details=details,
            )
        finally:
            sync_state.save()
            if sync.type == "playlist_cache":
                self._refresh_shared_playlist_cache(force=True)

    @staticmethod
    def _run_details(
        *,
        mode: str,
        stage: Optional[str] = None,
        summary: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        details: Dict[str, object] = {"mode": mode}
        if stage:
            details["stage"] = stage
        if isinstance(summary, dict):
            details.update(summary)
        return details

    # ------------------------------------------------------------------
    # Hot reload & lifecycle helpers
    # ------------------------------------------------------------------
    def _start_hot_reload_watcher(self) -> None:
        if self._hot_reload_thread and self._hot_reload_thread.is_alive():
            return

        def _watch() -> None:
            paths = {str(self.paths.syncs_dir), str(self.paths.global_config)}
            for changes in watch(*paths, raise_interrupt=False, stop_event=self._stop_event):
                self.logger.info("supervisor.config_change_detected", changes=list(changes))
                self._reload_configuration()

        self._hot_reload_thread = threading.Thread(target=_watch, name="spotifreak-hot-reload", daemon=True)
        self._hot_reload_thread.start()

    def _reload_configuration(self) -> None:
        with self._jobs_lock:
            try:
                new_config = load_global_config(self.paths.global_config)
                new_syncs = load_sync_configs(self.paths.syncs_dir)
            except Exception as exc:  # pragma: no cover - runtime config errors
                self.logger.error("supervisor.reload_failed", error=str(exc))
                return

            self.config = new_config
            new_index = {sync.id: sync for sync in new_syncs}

            removed = set(self._sync_index) - set(new_index)
            added = set(new_index) - set(self._sync_index)
            common = set(new_index) & set(self._sync_index)

            for sync_id in removed:
                try:
                    self._scheduler.remove_job(sync_id)
                except Exception:
                    pass
                self.logger.info("supervisor.sync_removed", sync_id=sync_id)

            for sync_id in common:
                if new_index[sync_id] != self._sync_index[sync_id]:
                    self._sync_index[sync_id] = new_index[sync_id]
                    self._register_sync_job(new_index[sync_id], immediate=True)

            for sync_id in added:
                sync = new_index[sync_id]
                self._sync_index[sync_id] = sync
                self._register_sync_job(sync, immediate=True)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:  # pragma: no cover - OS signal handling
        self.logger.info("supervisor.signal", signal=signum)
        self._stop_event.set()

    def shutdown(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()

        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        if self._ipc_thread and self._ipc_thread.is_alive():
            # close socket to unblock thread
            try:
                if self._ipc_socket and self._ipc_socket.exists():
                    self._ipc_socket.unlink()
            except OSError:
                pass
            self._ipc_thread.join(timeout=2)

        if self._hot_reload_thread and self._hot_reload_thread.is_alive():
            self._hot_reload_thread.join(timeout=2)

        self.logger.info("supervisor.shutdown")

    # ------------------------------------------------------------------
    # IPC server
    # ------------------------------------------------------------------
    def _start_ipc_server(self) -> None:
        if self._ipc_thread and self._ipc_thread.is_alive():
            return

        if not self._ipc_socket:
            return

        socket_path = self._ipc_socket
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if socket_path.exists():
                socket_path.unlink()
        except OSError:
            pass

        def _serve():
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as server:
                try:
                    server.bind(str(socket_path))
                except OSError as exc:
                    self.logger.error(
                        "supervisor.ipc_bind_failed",
                        error=str(exc),
                        socket=str(socket_path),
                    )
                    return
                server.listen(5)
                server.settimeout(1)
                while not self._stop_event.is_set():
                    try:
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    with closing(client):
                        try:
                            data = client.recv(65536)
                            if not data:
                                continue
                            request = json.loads(data.decode("utf-8"))
                            response = self._handle_ipc_command(request)
                        except Exception as exc:  # pragma: no cover - malformed request
                            response = {"status": "error", "message": str(exc)}
                        client.sendall(json.dumps(response).encode("utf-8"))

        self._ipc_thread = threading.Thread(target=_serve, name="spotifreak-ipc", daemon=True)
        self._ipc_thread.start()

    def _handle_ipc_command(self, request: Dict[str, object]) -> Dict[str, object]:
        command = str(request.get("command", "")).lower()
        if command == "status":
            return {"status": "ok", "jobs": self._job_snapshot()}
        if command in {"pause", "resume", "delete", "start"}:
            sync_id = request.get("sync_id")
            if not sync_id or sync_id not in self._sync_index:
                return {"status": "error", "message": f"Unknown sync: {sync_id}"}
            if command == "pause":
                self._scheduler.pause_job(sync_id)
                return {"status": "ok", "message": f"Paused {sync_id}"}
            if command == "resume":
                self._scheduler.resume_job(sync_id)
                return {"status": "ok", "message": f"Resumed {sync_id}"}
            if command == "delete":
                self._scheduler.remove_job(sync_id)
                return {"status": "ok", "message": f"Removed {sync_id}"}
            if command == "start":
                sync = self._sync_index[sync_id]
                self._register_sync_job(sync, immediate=True)
                return {"status": "ok", "message": f"Triggered {sync_id}"}

        return {"status": "error", "message": f"Unsupported command: {command}"}

    def _job_snapshot(self) -> List[Dict[str, object]]:
        jobs = []
        now = datetime.now(self._timezone)
        for job in self._scheduler.get_jobs():
            next_run = job.next_run_time
            jobs.append(
                {
                    "id": job.id,
                    "next_run": next_run.isoformat() if next_run else None,
                    "missed": next_run < now if next_run else False,
                    "paused": job.next_run_time is None,
                }
            )
        return jobs

    def _refresh_shared_playlist_cache(self, *, force: bool = False) -> Optional[dict]:
        if not self._playlist_cache_syncs:
            self._shared_playlist_cache = None
            return None

        best_cache: Optional[Tuple[datetime, dict]] = None

        for sync in self._playlist_cache_syncs:
            path = state_path_for_sync(self.paths, self.config, sync)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue

            mtime = stat.st_mtime
            cache_key = str(path)
            if not force and self._shared_playlist_cache is not None:
                last_mtime = self._playlist_cache_mtimes.get(cache_key)
                if last_mtime is not None and last_mtime >= mtime:
                    continue

            state = load_state(path)
            data = state.data or {}
            playlists = data.get("playlists")
            if not isinstance(playlists, list):
                continue

            refreshed_raw = data.get("last_refreshed")
            try:
                refreshed_at = datetime.fromisoformat(refreshed_raw) if refreshed_raw else None
            except ValueError:
                refreshed_at = None

            if refreshed_at is None:
                refreshed_at = datetime.fromtimestamp(mtime, tz=timezone.utc)

            if best_cache is None or refreshed_at > best_cache[0]:
                best_cache = (refreshed_at, {
                    "playlists": playlists,
                    "last_refreshed": refreshed_at.isoformat(),
                })
            self._playlist_cache_mtimes[cache_key] = mtime

        if best_cache is None:
            return self._shared_playlist_cache

        refreshed_at, payload = best_cache
        playlists = payload.get("playlists", [])
        by_name: Dict[str, dict] = {}
        by_id: Dict[str, dict] = {}
        for entry in playlists:
            if not isinstance(entry, dict):
                continue
            playlist_id = entry.get("id")
            name = entry.get("name", "")
            if playlist_id:
                by_id[str(playlist_id)] = entry
            if isinstance(name, str):
                by_name[name.strip().lower()] = entry
        self._shared_playlist_cache = {
            "last_refreshed": refreshed_at.isoformat(),
            "playlists": playlists,
            "by_name": by_name,
            "by_id": by_id,
        }
        return self._shared_playlist_cache
