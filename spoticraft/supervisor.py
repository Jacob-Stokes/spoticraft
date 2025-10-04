"""Supervisor runtime scaffolding for Spoticraft."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .auth import SpotifyClientFactory
from .config import ConfigPaths, GlobalConfig, SyncConfig
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

    def __post_init__(self) -> None:
        self._timezone, self._timezone_source = self._resolve_timezone(self.config.runtime.timezone)

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
            self.logger.warning(
                "supervisor.hot_reload_unavailable",
                message="Hot reload support not yet implemented; changes require restart.",
            )

        if sync_count == 0:
            self.logger.warning(
                "supervisor.no_syncs",
                message="No syncs defined. Supervisor will idle until exit.",
            )

        scheduler = self._create_scheduler()
        jobs_registered = 0

        for sync in self.syncs:
            module_logger = self.logger.bind(sync_id=sync.id, sync_type=sync.type)
            try:
                trigger = self._build_trigger(sync)
            except ValueError as exc:
                module_logger.error(
                    "supervisor.schedule_invalid",
                    error=str(exc),
                )
                continue

            try:
                scheduler.add_job(
                    self._run_sync,
                    trigger=trigger,
                    id=sync.id,
                    name=f"{sync.type}:{sync.id}",
                    args=[sync],
                    coalesce=True,
                    max_instances=1,
                    replace_existing=True,
                    next_run_time=datetime.now(self._timezone),
                )
            except Exception as exc:  # pragma: no cover - APScheduler edge conditions
                module_logger.exception(
                    "supervisor.job_registration_failed",
                    error=str(exc),
                )
                continue

            jobs_registered += 1
            module_logger.info(
                "supervisor.sync_scheduled",
                schedule=str(trigger),
            )

        self.logger.info(
            "supervisor.scheduler_starting",
            jobs=jobs_registered,
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:  # pragma: no cover - manual shutdown
            self.logger.info("supervisor.stop", reason="keyboard_interrupt")
        finally:
            if scheduler.running:  # pragma: no cover - depends on runtime state
                scheduler.shutdown(wait=False)
            self.logger.info("supervisor.shutdown")

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
        return BlockingScheduler(timezone=self._timezone, executors=executors)

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

    def _run_sync(self, sync: SyncConfig) -> None:
        module_logger = self.logger.bind(sync_id=sync.id, sync_type=sync.type)
        state_path = state_path_for_sync(self.paths, self.config, sync)
        sync_state = load_state(state_path)

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
