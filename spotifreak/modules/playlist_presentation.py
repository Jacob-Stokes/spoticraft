"""Playlist presentation module for rotating covers, titles, and descriptions."""

from __future__ import annotations

import base64
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional

import requests
from pydantic import BaseModel, Field, ValidationError, model_validator

from .base import SyncContext, SyncModule
from .playlist_mirror import PlaylistResolverConfig
from ..services.spotify_client import SpotifyService


class FeatureOptions(BaseModel):
    enabled: bool = False
    selection: Literal["sequential", "random"] = "sequential"
    assets: Dict[str, List[str]] = Field(default_factory=dict)

    @model_validator(mode="before")
    def normalize_assets(cls, value):  # type: ignore[override]
        if isinstance(value, list):
            return {"enabled": True, "assets": {"default": value}}
        if isinstance(value, dict):
            data = dict(value)
            assets = data.get("assets")
            if isinstance(assets, list):
                data["assets"] = {"default": assets}
            return data
        return value


class DescriptionOptions(FeatureOptions):
    use_dynamic: bool = False
    dynamic_templates: List[str] = Field(default_factory=list)


class CustomPhase(BaseModel):
    name: str
    start: str  # HH:MM


class SunriseOptions(BaseModel):
    latitude: float
    longitude: float
    morning_duration_hours: float = Field(default=3.0, ge=0)
    evening_duration_hours: float = Field(default=2.0, ge=0)
    night_offset_hours: float = Field(default=1.0, ge=0)


class PhasesOptions(BaseModel):
    mode: Literal["none", "sunrise_sunset", "custom"] = "none"
    sunrise: Optional[SunriseOptions] = None
    custom: List[CustomPhase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode(self) -> "PhasesOptions":
        if self.mode == "sunrise_sunset" and not self.sunrise:
            raise ValueError("sunrise options must be provided when mode is 'sunrise_sunset'")
        if self.mode == "custom" and not self.custom:
            raise ValueError("custom phases must be provided when mode is 'custom'")
        return self


class PlaylistPresentationOptions(BaseModel):
    playlist: PlaylistResolverConfig
    interval_seconds: Optional[int] = Field(default=None, ge=60)
    phases: Optional[PhasesOptions] = None
    cover: FeatureOptions = Field(default_factory=FeatureOptions)
    title: FeatureOptions = Field(default_factory=FeatureOptions)
    description: DescriptionOptions = Field(default_factory=DescriptionOptions)


@dataclass
class PlaylistPresentationModule(SyncModule):
    """Rotate playlist cover art, titles, and descriptions."""

    config: any

    def __init__(self, config):
        self.config = config
        try:
            self.options = PlaylistPresentationOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(f"Invalid playlist presentation options: {exc}") from exc
        self.last_run_summary: dict = {}

    def run(self, context: SyncContext) -> None:
        logger = context.logger
        service = context.spotify
        state = context.state
        if service is None or state is None:
            logger.error("presentation.no_spotify_client", message="Spotify client unavailable.")
            return

        try:
            playlist_id = self._resolve_playlist(service)
        except ValueError as exc:
            logger.error("presentation.playlist_error", error=str(exc))
            return

        if not any([
            self.options.cover.enabled,
            self.options.title.enabled,
            self.options.description.enabled,
        ]):
            logger.info("presentation.no_features_enabled")
            return

        now = datetime.now(timezone.utc)
        presentation_state = state.data.setdefault("playlist_presentation", {})
        last_update_iso = presentation_state.get("last_updated_at")
        effective_interval = self._determine_interval_seconds()

        if last_update_iso:
            try:
                last_update = datetime.fromisoformat(last_update_iso)
            except ValueError:
                last_update = None
            if last_update:
                elapsed = (now - last_update).total_seconds()
                if elapsed < effective_interval:
                    logger.info(
                        "presentation.interval_skip",
                        interval=effective_interval,
                        remaining=effective_interval - int(elapsed),
                    )
                    self._update_summary(status="skipped_interval", phase=presentation_state.get("last_phase"))
                    return

        phase = self._determine_phase(now, presentation_state, state, context)
        presentation_state["last_phase"] = phase

        feature_state = presentation_state.setdefault("features", {})

        updates_applied = False

        cover_path = None
        if self.options.cover.enabled:
            cover_path = self._select_asset(self.options.cover, feature_state.setdefault("cover", {}), phase)
            if cover_path:
                try:
                    image_b64 = self._encode_image(cover_path, context)
                    service.upload_playlist_cover(playlist_id, image_b64)
                    updates_applied = True
                    logger.info("presentation.cover_updated", path=str(cover_path), phase=phase)
                except Exception as exc:  # pragma: no cover - network/filesystem issues
                    logger.error("presentation.cover_failed", error=str(exc), path=str(cover_path))

        title_value = None
        if self.options.title.enabled:
            title_value = self._select_asset(
                self.options.title,
                feature_state.setdefault("title", {}),
                phase,
            )

        description_value = None
        if self.options.description.enabled:
            description_assets = self._assets_for_phase(self.options.description.assets, phase)
            dynamic = []
            if self.options.description.use_dynamic:
                templates = self.options.description.dynamic_templates or self._default_description_templates()
                dynamic = self._render_dynamic_descriptions(templates)
            combined = description_assets + dynamic
            if combined:
                desc_options = DescriptionOptions(
                    enabled=True,
                    selection=self.options.description.selection,
                    assets={phase: combined},
                )
                description_value = self._select_asset(
                    desc_options,
                    feature_state.setdefault("description", {}),
                    phase,
                )

        details_update = {}
        last_detail_state = feature_state.setdefault("details", {})
        if title_value and title_value != last_detail_state.get("title"):
            details_update["name"] = title_value
            last_detail_state["title"] = title_value
        if description_value and description_value != last_detail_state.get("description"):
            details_update["description"] = description_value
            last_detail_state["description"] = description_value

        if details_update:
            try:
                service.update_playlist_details(playlist_id, **details_update)
                updates_applied = True
                logger.info(
                    "presentation.details_updated",
                    fields=list(details_update.keys()),
                    phase=phase,
                )
            except Exception as exc:  # pragma: no cover - API failure
                logger.error("presentation.details_failed", error=str(exc))

        if updates_applied:
            presentation_state["last_updated_at"] = now.isoformat()
            state._dirty = True
            self._update_summary(status="updated", phase=phase, fields=list(details_update.keys()))
        else:
            self._update_summary(status="noop", phase=phase)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
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

    def _determine_phase(
        self,
        now: datetime,
        presentation_state: dict,
        state,
        context: SyncContext,
    ) -> str:
        options = self.options.phases
        if not options or options.mode == "none":
            return "default"

        if options.mode == "custom":
            schedule = self._build_custom_schedule(options.custom, now)
            return self._phase_from_schedule(schedule, now, default="default")

        if options.mode == "sunrise_sunset" and options.sunrise:
            schedule_state = presentation_state.setdefault("phase_schedule", {})
            cached_date = schedule_state.get("date")
            today_str = now.astimezone().date().isoformat()
            if cached_date != today_str:
                schedule = self._compute_sunrise_schedule(options.sunrise, now, context)
                if schedule:
                    schedule_state["date"] = today_str
                    schedule_state["times"] = {k: v.isoformat() for k, v in schedule.items()}
                    state._dirty = True
                else:
                    return "default"
            times = schedule_state.get("times", {})
            schedule = {
                phase: datetime.fromisoformat(value)
                for phase, value in times.items()
                if isinstance(value, str)
            }
            if not schedule:
                return "default"
            return self._phase_from_schedule(schedule, now, default="default")

        return "default"

    def _build_custom_schedule(self, phases: List[CustomPhase], now: datetime) -> Dict[str, datetime]:
        tz = now.astimezone().tzinfo
        today = now.astimezone().date()
        schedule: Dict[str, datetime] = {}
        for phase in phases:
            try:
                hour, minute = [int(part) for part in phase.start.split(":", 1)]
            except ValueError:
                continue
            start = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)
            schedule[phase.name] = start
        return schedule

    def _compute_sunrise_schedule(
        self,
        options: SunriseOptions,
        now: datetime,
        context: SyncContext,
    ) -> Optional[Dict[str, datetime]]:
        params = {
            "lat": str(options.latitude),
            "lng": str(options.longitude),
            "formatted": "0",
        }
        try:
            response = requests.get("https://api.sunrise-sunset.org/json", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pragma: no cover - network failure
            if context.logger:
                context.logger.warning("presentation.sunrise_api_failed", error=str(exc))
            return None

        if data.get("status") != "OK":
            return None

        tz = now.astimezone().tzinfo
        sunrise_utc = datetime.fromisoformat(data["results"]["sunrise"].replace("Z", "+00:00"))
        sunset_utc = datetime.fromisoformat(data["results"]["sunset"].replace("Z", "+00:00"))
        sunrise = sunrise_utc.astimezone(tz)
        sunset = sunset_utc.astimezone(tz)

        morning_start = sunrise
        day_start = sunrise + timedelta(hours=options.morning_duration_hours)
        evening_start = sunset - timedelta(hours=options.evening_duration_hours)
        night_start = sunset + timedelta(hours=options.night_offset_hours)

        return {
            "morning": morning_start,
            "day": day_start,
            "evening": evening_start,
            "night": night_start,
        }

    def _phase_from_schedule(
        self,
        schedule: Dict[str, datetime],
        now: datetime,
        default: str = "default",
    ) -> str:
        if not schedule:
            return default
        sorted_items = sorted(schedule.items(), key=lambda item: item[1])
        day_seconds = 86400
        for idx, (name, start) in enumerate(sorted_items):
            next_start = sorted_items[(idx + 1) % len(sorted_items)][1]
            interval = (next_start - start).total_seconds()
            if interval <= 0:
                interval += day_seconds

            delta = (now - start).total_seconds()
            if delta < 0:
                delta += day_seconds

            if 0 <= delta < interval:
                return name
        return sorted_items[-1][0]

    def _select_asset(self, options: FeatureOptions, feature_state: dict, phase: str) -> Optional[str]:
        assets = self._assets_for_phase(options.assets, phase)
        if not assets:
            return None

        if options.selection == "random":
            candidate = random.choice(assets)
            if len(assets) > 1:
                last = feature_state.get("last")
                attempts = 0
                while candidate == last and attempts < 5:
                    candidate = random.choice(assets)
                    attempts += 1
        else:
            indices = feature_state.setdefault("indices", {})
            current = indices.get(phase, 0)
            candidate = assets[current % len(assets)]
            indices[phase] = current + 1

        if candidate == feature_state.get("last"):
            return None

        feature_state["last"] = candidate
        return candidate

    def _assets_for_phase(self, assets: Dict[str, List[str]], phase: str) -> List[str]:
        if phase in assets and assets[phase]:
            return assets[phase]
        if "default" in assets and assets["default"]:
            return assets["default"]
        # flatten remaining phases
        flattened: List[str] = []
        for values in assets.values():
            flattened.extend(values)
        return flattened

    def _encode_image(self, raw_path: str, context: SyncContext) -> str:
        path = Path(raw_path).expanduser()
        if not path.is_absolute() and context.paths:
            path = context.paths.base_dir / path
        data = path.read_bytes()
        encoded = base64.b64encode(data).decode("utf-8")
        return encoded

    def _render_dynamic_descriptions(self, templates: List[str]) -> List[str]:
        now_local = datetime.now().astimezone()
        replacements = {
            "{time}": now_local.strftime("%H:%M"),
            "{date}": now_local.strftime("%B %d, %Y"),
            "{weekday}": now_local.strftime("%A"),
        }
        results: List[str] = []
        for template in templates:
            text = template
            for key, value in replacements.items():
                text = text.replace(key, value)
            results.append(text)
        return results

    def _default_description_templates(self) -> List[str]:
        return [
            "Updated at {time} on {weekday}",
            "Current vibe as of {date}",
            "Live update – {time}",
        ]

    def _update_summary(self, **fields) -> None:
        if not isinstance(self.last_run_summary, dict):
            self.last_run_summary = {}
        self.last_run_summary.update(fields)

    def _determine_interval_seconds(self) -> int:
        if self.options.interval_seconds:
            return self.options.interval_seconds

        interval_expr = getattr(self.config.schedule, "interval", None)
        if interval_expr:
            seconds = self._parse_interval_expression(interval_expr)
            if seconds:
                return seconds
        # Fallback to five minutes if nothing else specified
        return 300

    def _parse_interval_expression(self, expression: str) -> Optional[int]:
        pattern = re.compile(r"(\d+)([smhd])", re.IGNORECASE)
        total = 0
        pos = 0
        expr = expression.strip()
        for match in pattern.finditer(expr):
            if match.start() != pos:
                return None
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
            pos = match.end()
        if pos != len(expr):
            return None
        return total or None
