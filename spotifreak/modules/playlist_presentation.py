"""Playlist presentation module for rotating covers, titles, and descriptions."""

from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .base import SyncContext, SyncModule
from .playlist_mirror import PlaylistResolverConfig
from ..services.spotify_client import SpotifyService


@dataclass
class AssetCandidate:
    """Represents a single asset option with optional weighting metadata."""

    value: str
    weight: float = 1.0
    source_id: str = ""


@dataclass
class FeatureOutcome:
    """Selection result for a feature update."""

    should_apply: bool
    value: Optional[str]
    reason: Optional[str] = None


class FeatureSelection(BaseModel):
    """Controls how assets are picked for a feature run."""

    mode: Literal["sequential", "random", "weighted_random", "round_robin"] = "sequential"
    dedupe_window: int = Field(default=0, ge=0)
    restart_policy: Literal["loop", "bounce", "random_restart"] = "loop"
    group_key: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class FeatureCadence(BaseModel):
    """Cadence controls for feature execution frequency."""

    multiplier: int = Field(default=1, ge=1)
    phase_overrides: Dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")


class AssetSource(BaseModel):
    """Definition of where to pull assets for a feature."""

    type: Literal["list", "folder", "fallback"] = "list"
    items: List[str] = Field(default_factory=list)
    path: Optional[str] = None
    pattern: Optional[str] = None
    recursive: bool = False
    shuffle_on_load: bool = False
    max_items: Optional[int] = Field(default=None, ge=1)
    weight: float = 1.0
    cache_ttl_seconds: int = Field(default=300, ge=0)

    model_config = ConfigDict(extra="ignore")


class FeatureOptions(BaseModel):
    enabled: bool = False
    selection: FeatureSelection = Field(default_factory=FeatureSelection)
    sources: Dict[str, List[AssetSource]] = Field(default_factory=dict)
    fallback_asset: Optional[str] = None
    failure_mode: Literal["skip", "reuse_last", "stop"] = "skip"
    cadence: FeatureCadence = Field(default_factory=FeatureCadence)
    assets: Dict[str, List[str]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    def _normalise_legacy_schema(cls, value: Any):  # type: ignore[override]
        """Coerce legacy configuration shapes into the newer feature schema."""
        # Allow bare lists (legacy "assets" shorthand)
        if isinstance(value, list):
            return {"enabled": True, "assets": {"default": value}}

        if not isinstance(value, dict):
            return value

        data = dict(value)

        # Legacy selection strings -> structured configuration
        selection = data.get("selection")
        if isinstance(selection, str):
            data["selection"] = {"mode": selection}

        # Legacy assets list -> default bucket
        assets = data.get("assets")
        if isinstance(assets, list):
            data["assets"] = {"default": assets}

        # Normalise sources entries so downstream parsing is predictable
        raw_sources = data.get("sources")
        if isinstance(raw_sources, dict):
            normalised_sources: Dict[str, List[Any]] = {}
            for phase, entries in raw_sources.items():
                items: List[Any]
                if isinstance(entries, list):
                    items = []
                    for entry in entries:
                        if isinstance(entry, str):
                            items.append({"type": "list", "items": [entry]})
                        else:
                            items.append(entry)
                else:
                    items = [entries]
                normalised_sources[phase] = items
            data["sources"] = normalised_sources

        return data

    @model_validator(mode="after")
    def _populate_sources(self) -> "FeatureOptions":
        """Ensure feature sources are available as fully parsed objects."""
        if not self.sources and self.assets:
            converted: Dict[str, List[AssetSource]] = {}
            for phase, items in self.assets.items():
                converted[phase] = [AssetSource(type="list", items=[str(item) for item in items])]
            self.sources = converted
        elif self.sources:
            # Ensure nested lists are parsed into AssetSource instances
            parsed: Dict[str, List[AssetSource]] = {}
            for phase, entries in self.sources.items():
                parsed[phase] = [AssetSource.model_validate(entry) if not isinstance(entry, AssetSource) else entry for entry in entries]
            self.sources = parsed

        return self


class DescriptionOptions(FeatureOptions):
    use_dynamic: bool = False
    dynamic_templates: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


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
        """Validate that auxiliary settings exist for the selected phase mode."""
        if self.mode == "sunrise_sunset" and not self.sunrise:
            raise ValueError("sunrise options must be provided when mode is 'sunrise_sunset'")
        if self.mode == "custom" and not self.custom:
            raise ValueError("custom phases must be provided when mode is 'custom'")
        return self


class PlaylistPresentationOptions(BaseModel):
    playlist: PlaylistResolverConfig
    interval_seconds: Optional[int] = Field(default=None, ge=1)
    phases: Optional[PhasesOptions] = None
    cover: FeatureOptions = Field(default_factory=FeatureOptions)
    title: FeatureOptions = Field(default_factory=FeatureOptions)
    description: DescriptionOptions = Field(default_factory=DescriptionOptions)
    random_seed: Optional[str] = None


@dataclass
class PlaylistPresentationModule(SyncModule):
    """Rotate playlist cover art, titles, and descriptions."""

    config: any

    def __init__(self, config):
        """Validate provided sync configuration and cache derived options."""
        self.config = config
        try:
            self.options = PlaylistPresentationOptions.model_validate(config.options)
        except ValidationError as exc:
            raise ValueError(f"Invalid playlist presentation options: {exc}") from exc
        self.last_run_summary: dict = {}

    def run(self, context: SyncContext) -> None:
        """Execute a single presentation update cycle for the target playlist."""
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
        effective_interval = self._determine_interval_seconds()

        if not self._should_execute_now(presentation_state, now, effective_interval):
            remaining = self._remaining_interval(presentation_state, now, effective_interval)
            logger.info(
                "presentation.interval_skip",
                interval=effective_interval,
                remaining=remaining,
            )
            self._update_summary(status="skipped_interval", phase=presentation_state.get("last_phase"))
            return

        phase = self._determine_phase(now, presentation_state, state, context)
        presentation_state["last_phase"] = phase
        presentation_state["global_run_count"] = presentation_state.get("global_run_count", 0) + 1

        rng = random.Random()
        if self.options.random_seed:
            rng.seed(f"{self.options.random_seed}:{presentation_state['global_run_count']}")

        features_state = presentation_state.setdefault("features", {})
        groups_state = presentation_state.setdefault("groups", {})
        cache_state = presentation_state.setdefault("source_cache", {})

        updates_applied = False
        state_dirty = False

        cover_outcome = self._determine_feature_outcome(
            feature_name="cover",
            options=self.options.cover,
            presentation_state=presentation_state,
            features_state=features_state,
            groups_state=groups_state,
            cache_state=cache_state,
            phase=phase,
            now=now,
            context=context,
            rng=rng,
        )

        if cover_outcome.should_apply and cover_outcome.value:
            try:
                image_b64 = self._encode_image(cover_outcome.value, context)
                service.upload_playlist_cover(playlist_id, image_b64)
                updates_applied = True
                state_dirty = True
                logger.info("presentation.cover_updated", path=str(cover_outcome.value), phase=phase)
            except Exception as exc:  # pragma: no cover - network/filesystem issues
                if not self._handle_failure("cover", self.options.cover, features_state.get("cover", {}), phase, exc, logger):
                    raise
        elif cover_outcome.reason:
            self._update_summary(cover_status="skip", cover_reason=cover_outcome.reason, phase=phase)

        title_outcome = self._determine_feature_outcome(
            feature_name="title",
            options=self.options.title,
            presentation_state=presentation_state,
            features_state=features_state,
            groups_state=groups_state,
            cache_state=cache_state,
            phase=phase,
            now=now,
            context=context,
            rng=rng,
        )

        description_outcome = self._determine_feature_outcome(
            feature_name="description",
            options=self.options.description,
            presentation_state=presentation_state,
            features_state=features_state,
            groups_state=groups_state,
            cache_state=cache_state,
            phase=phase,
            now=now,
            context=context,
            rng=rng,
            is_description=True,
        )

        details_update: Dict[str, str] = {}
        last_detail_state = features_state.setdefault("details", {})

        if title_outcome.should_apply and title_outcome.value:
            if title_outcome.value != last_detail_state.get("title"):
                details_update["name"] = title_outcome.value
                last_detail_state["title"] = title_outcome.value
                state_dirty = True
        elif title_outcome.reason:
            self._update_summary(title_status="skip", title_reason=title_outcome.reason, phase=phase)

        if description_outcome.should_apply and description_outcome.value:
            if description_outcome.value != last_detail_state.get("description"):
                details_update["description"] = description_outcome.value
                last_detail_state["description"] = description_outcome.value
                state_dirty = True
        elif description_outcome.reason:
            self._update_summary(description_status="skip", description_reason=description_outcome.reason, phase=phase)

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
                if not self._handle_failure("details", self.options.title, features_state.get("title", {}), phase, exc, logger):
                    logger.error("presentation.details_failed", error=str(exc))

        if updates_applied:
            presentation_state["last_updated_at"] = now.isoformat()
            state_dirty = True
            self._update_summary(status="updated", phase=phase, fields=list(details_update.keys()) or ["cover"])
        else:
            self._update_summary(status="noop", phase=phase)

        if state_dirty:
            state._dirty = True

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _resolve_playlist(self, service: SpotifyService) -> str:
        """Resolve or create the playlist ID backing this presentation."""
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
        """Determine which phase (morning, day, etc.) should drive assets now."""
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
        """Translate custom phase definitions into concrete datetimes for today."""
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
        """Fetch sunrise/sunset data and calculate morning/day/evening/night spans."""
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
        """Pick the current phase label by comparing now against schedule windows."""
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

    def _should_execute_now(self, presentation_state: dict, now: datetime, interval: int) -> bool:
        """Check whether enough time has elapsed to perform another update."""
        last_iso = presentation_state.get("last_updated_at")
        if not last_iso:
            return True
        try:
            last_run = datetime.fromisoformat(last_iso)
        except ValueError:
            return True
        elapsed = (now - last_run).total_seconds()
        return elapsed >= interval

    def _remaining_interval(self, presentation_state: dict, now: datetime, interval: int) -> int:
        """Return remaining seconds before the next eligible run."""
        last_iso = presentation_state.get("last_updated_at")
        if not last_iso:
            return 0
        try:
            last_run = datetime.fromisoformat(last_iso)
        except ValueError:
            return 0
        remaining = interval - (now - last_run).total_seconds()
        return max(int(remaining), 0)

    def _determine_feature_outcome(
        self,
        *,
        feature_name: str,
        options: FeatureOptions,
        presentation_state: dict,
        features_state: dict,
        groups_state: dict,
        cache_state: dict,
        phase: str,
        now: datetime,
        context: SyncContext,
        rng: random.Random,
        is_description: bool = False,
    ) -> "FeatureOutcome":
        """Resolve whether a feature should update and which asset to apply."""
        if not options.enabled:
            return FeatureOutcome(False, None, "disabled")

        feature_state = self._resolve_feature_state(feature_name, options, features_state, groups_state)
        feature_state["run_count"] = feature_state.get("run_count", 0) + 1

        if not self._within_feature_cadence(options.cadence, feature_state, phase, now, presentation_state):
            return FeatureOutcome(False, None, "cadence_skip")

        candidates, bucket_map = self._collect_candidates(
            feature_name=feature_name,
            options=options,
            phase=phase,
            context=context,
            cache_state=cache_state,
            rng=rng,
        )

        if is_description and options.enabled:
            if isinstance(options, DescriptionOptions) and options.use_dynamic:
                templates = options.dynamic_templates or self._default_description_templates()
                for text in self._render_dynamic_descriptions(templates):
                    candidates.append(AssetCandidate(value=text, weight=1.0, source_id="dynamic"))

        if not candidates:
            if options.fallback_asset:
                return FeatureOutcome(True, options.fallback_asset, "fallback_asset")
            return FeatureOutcome(False, None, "no_assets")

        outcome = self._select_candidate(
            feature_name=feature_name,
            options=options,
            feature_state=feature_state,
            groups_state=groups_state,
            presentation_state=presentation_state,
            candidates=candidates,
            bucket_map=bucket_map,
            phase=phase,
            rng=rng,
        )

        if not outcome:
            if options.fallback_asset:
                return FeatureOutcome(True, options.fallback_asset, "fallback_asset")
            return FeatureOutcome(False, None, "selection_failed")

        feature_state["last_value"] = outcome
        feature_state.setdefault("history", []).append(outcome)
        self._trim_history(feature_state["history"], options.selection.dedupe_window)

        return FeatureOutcome(True, outcome)

    def _resolve_feature_state(
        self,
        feature_name: str,
        options: FeatureOptions,
        features_state: dict,
        groups_state: dict,
    ) -> dict:
        """Return per-feature state, optionally shared across grouped features."""
        group_key = options.selection.group_key
        if not group_key:
            return features_state.setdefault(feature_name, {})

        group_state = groups_state.setdefault(group_key, {})
        state_dict = group_state.setdefault("state", {})
        features_state[feature_name] = state_dict
        return state_dict

    def _within_feature_cadence(
        self,
        cadence: FeatureCadence,
        feature_state: dict,
        phase: str,
        now: datetime,
        presentation_state: dict,
    ) -> bool:
        """Determine if cadence rules allow this feature to change this run."""
        global_count = presentation_state.get("global_run_count", 1)
        if cadence.multiplier > 1 and global_count % cadence.multiplier != 0:
            return False

        phase_intervals = cadence.phase_overrides or {}
        if phase in phase_intervals:
            last_iso = feature_state.get("last_value_at")
            if last_iso:
                try:
                    last_time = datetime.fromisoformat(last_iso)
                except ValueError:
                    return True
                elapsed = (now - last_time).total_seconds()
                if elapsed < phase_intervals[phase]:
                    return False
        return True

    def _collect_candidates(
        self,
        *,
        feature_name: str,
        options: FeatureOptions,
        phase: str,
        context: SyncContext,
        cache_state: dict,
        rng: random.Random,
    ) -> Tuple[List[AssetCandidate], Dict[str, List[str]]]:
        """Build candidate asset list for the feature and phase."""
        phase_sources = list(options.sources.get(phase, []))
        default_sources = options.sources.get("default", [])
        if phase != "default":
            phase_sources.extend(default_sources)

        candidates: List[AssetCandidate] = []
        buckets: Dict[str, List[str]] = {}
        fallbacks: List[AssetCandidate] = []

        for index, source in enumerate(phase_sources):
            source_id = f"{feature_name}:{phase}:{index}:{source.type}:{source.path or '-'}"
            items = self._load_source_assets(source_id, source, context, cache_state, rng)
            if not items:
                continue
            if source.shuffle_on_load and len(items) > 1:
                rng.shuffle(items)
            if source.max_items:
                items = items[: source.max_items]

            if source.type == "fallback":
                fallbacks.extend(AssetCandidate(value=item, weight=source.weight, source_id=source_id) for item in items)
                continue

            bucket = buckets.setdefault(source_id, [])
            for item in items:
                bucket.append(item)
                candidates.append(AssetCandidate(value=item, weight=source.weight, source_id=source_id))

        if not candidates:
            if fallbacks:
                return fallbacks, {candidate.source_id: [candidate.value] for candidate in fallbacks}
            if options.fallback_asset:
                candidate = AssetCandidate(value=options.fallback_asset, source_id=f"{feature_name}:fallback")
                return [candidate], {candidate.source_id: [candidate.value]}

        return candidates, buckets

    def _load_source_assets(
        self,
        cache_key: str,
        source: AssetSource,
        context: SyncContext,
        cache_state: dict,
        rng: random.Random,
    ) -> List[str]:
        """Resolve raw asset values from a source definition."""
        if source.type == "list":
            return [str(item) for item in source.items]

        if source.type == "folder":
            cached = cache_state.get(cache_key)
            now_ts = time.time()
            if cached and source.cache_ttl_seconds > 0:
                if (now_ts - cached.get("timestamp", 0)) <= source.cache_ttl_seconds:
                    return list(cached.get("items", []))

            folder_path = Path(source.path or "").expanduser()
            base_dir = context.paths.base_dir if context.paths else Path.cwd()
            if not folder_path.is_absolute():
                folder_path = base_dir / folder_path

            pattern = source.pattern or "*"
            paths: List[str] = []
            if folder_path.is_dir():
                iterator: Iterable[Path]
                if source.recursive:
                    iterator = folder_path.rglob(pattern)
                else:
                    iterator = folder_path.glob(pattern)
                for child in iterator:
                    if not child.is_file():
                        continue
                    try:
                        relative = child.relative_to(base_dir)
                        paths.append(str(relative))
                    except ValueError:
                        paths.append(str(child))

            cache_state[cache_key] = {"timestamp": now_ts, "items": list(paths)}
            return paths

        if source.type == "fallback":
            return [str(item) for item in source.items]

        return []

    def _select_candidate(
        self,
        *,
        feature_name: str,
        options: FeatureOptions,
        feature_state: dict,
        groups_state: dict,
        presentation_state: dict,
        candidates: List[AssetCandidate],
        bucket_map: Dict[str, List[str]],
        phase: str,
        rng: random.Random,
    ) -> Optional[str]:
        """Pick the winning asset while honouring selection strategy and grouping."""
        group_key = options.selection.group_key
        group_state: Optional[dict] = None
        if group_key:
            group_state = groups_state.setdefault(group_key, {})
            run_marker = presentation_state.get("global_run_count", 0)
            cache = group_state.setdefault("cache", {})
            cached = cache.get((phase, run_marker))
            if cached is not None:
                return cached

        selection_mode = options.selection.mode
        value: Optional[str] = None

        if selection_mode == "sequential":
            value = self._select_sequential(candidates, feature_state, options.selection, rng)
        elif selection_mode == "random":
            value = self._select_random(candidates, feature_state, options.selection, rng)
        elif selection_mode == "weighted_random":
            value = self._select_weighted_random(candidates, feature_state, options.selection, rng)
        elif selection_mode == "round_robin":
            value = self._select_round_robin(bucket_map, candidates, feature_state)

        if value is None:
            return None

        history = feature_state.setdefault("history", [])
        if options.selection.dedupe_window > 0 and value in history[-options.selection.dedupe_window :]:
            # attempt to find alternative for random modes
            if selection_mode in {"random", "weighted_random"}:
                alt = self._select_random_alternative(candidates, history, options.selection.dedupe_window, rng)
                if alt:
                    value = alt
            elif selection_mode == "sequential":
                value = self._select_sequential(candidates, feature_state, options.selection, rng, force_next=True)

        if group_key and group_state is not None:
            cache = group_state.setdefault("cache", {})
            cache[(phase, presentation_state.get("global_run_count", 0))] = value

        feature_state["last_value_at"] = datetime.now(timezone.utc).isoformat()
        return value

    @staticmethod
    def _trim_history(history: List[str], window: int) -> None:
        """Keep history bounded to avoid unbounded growth."""
        if window <= 0:
            history.clear()
            return
        if len(history) > window * 2:
            del history[:-window * 2]

    def _select_sequential(
        self,
        candidates: Sequence[AssetCandidate],
        feature_state: dict,
        selection: FeatureSelection,
        rng: random.Random,
        *,
        force_next: bool = False,
    ) -> Optional[str]:
        """Walk candidates in order, supporting bounce and restart behaviour."""
        if not candidates:
            return None

        values = [candidate.value for candidate in candidates]
        cursor = feature_state.get("cursor", 0)
        direction = feature_state.get("direction", 1)

        if selection.restart_policy == "random_restart" and cursor >= len(values):
            cursor = rng.randrange(len(values))
        elif selection.restart_policy == "bounce":
            if cursor >= len(values) or cursor < 0:
                direction = -direction
                cursor = max(0, min(len(values) - 1, cursor + direction))
        else:
            cursor = cursor % len(values)

        if force_next:
            cursor = (cursor + 1) % len(values)

        feature_state["cursor"] = cursor + direction
        feature_state["direction"] = direction
        return values[cursor]

    def _select_random(
        self,
        candidates: Sequence[AssetCandidate],
        feature_state: dict,
        selection: FeatureSelection,
        rng: random.Random,
    ) -> Optional[str]:
        """Select a random candidate with optional dedupe attempts."""
        if not candidates:
            return None
        values = [candidate.value for candidate in candidates]
        choice = rng.choice(values)
        if selection.dedupe_window > 0:
            history = feature_state.setdefault("history", [])
            attempts = 0
            while choice in history[-selection.dedupe_window :] and attempts < 5:
                choice = rng.choice(values)
                attempts += 1
        return choice

    def _select_random_alternative(
        self,
        candidates: Sequence[AssetCandidate],
        history: Sequence[str],
        window: int,
        rng: random.Random,
    ) -> Optional[str]:
        """Pick an alternate random candidate avoiding recent history."""
        values = [candidate.value for candidate in candidates if candidate.value not in history[-window:]]
        if not values:
            return None
        return rng.choice(values)

    def _select_weighted_random(
        self,
        candidates: Sequence[AssetCandidate],
        feature_state: dict,
        selection: FeatureSelection,
        rng: random.Random,
    ) -> Optional[str]:
        """Select a random candidate respecting per-item weights."""
        if not candidates:
            return None
        weights = [candidate.weight for candidate in candidates]
        total = sum(weights)
        if total <= 0:
            return self._select_random(candidates, feature_state, selection, rng)
        pick = rng.random() * total
        upto = 0.0
        for candidate, weight in zip(candidates, weights):
            upto += weight
            if pick <= upto:
                return candidate.value
        return candidates[-1].value

    def _select_round_robin(
        self,
        bucket_map: Dict[str, List[str]],
        candidates: Sequence[AssetCandidate],
        feature_state: dict,
    ) -> Optional[str]:
        """Cycle through buckets to distribute usage evenly across sources."""
        if not candidates:
            return None
        if not bucket_map:
            return candidates[feature_state.get("cursor", 0) % len(candidates)].value

        current_cycle = feature_state.get("round_robin_cycle")
        new_cycle = list(bucket_map.keys())
        if current_cycle != new_cycle:
            feature_state["round_robin_cycle"] = new_cycle
            feature_state["round_robin_pointer"] = 0
            feature_state["round_robin_indices"] = {}
        cycle = feature_state.setdefault("round_robin_cycle", new_cycle)
        pointer = feature_state.get("round_robin_pointer", 0)
        for _ in range(len(cycle)):
            source_id = cycle[pointer % len(cycle)]
            entries = bucket_map.get(source_id, [])
            index_map = feature_state.setdefault("round_robin_indices", {})
            idx = index_map.get(source_id, 0)
            if entries:
                value = entries[idx % len(entries)]
                index_map[source_id] = idx + 1
                feature_state["round_robin_pointer"] = pointer + 1
                return value
            pointer += 1
        return candidates[0].value

    def _handle_failure(
        self,
        feature_name: str,
        options: FeatureOptions,
        feature_state: dict,
        phase: str,
        exc: Exception,
        logger,
    ) -> bool:
        """Decide how to proceed after an asset upload/update failure."""
        mode = options.failure_mode
        if mode == "reuse_last":
            last_value = feature_state.get("last_value")
            if last_value:
                logger.warning(
                    "presentation.reuse_last", feature=feature_name, phase=phase, error=str(exc)
                )
                return True
            mode = "skip"

        if mode == "skip":
            logger.warning(
                "presentation.failure_skipped", feature=feature_name, phase=phase, error=str(exc)
            )
            return True

        return False


    def _encode_image(self, raw_path: str, context: SyncContext) -> str:
        """Load an image file from disk and return its base64 encoding."""
        path = Path(raw_path).expanduser()
        if not path.is_absolute() and context.paths:
            path = context.paths.base_dir / path
        data = path.read_bytes()
        encoded = base64.b64encode(data).decode("utf-8")
        return encoded

    def _render_dynamic_descriptions(self, templates: List[str]) -> List[str]:
        """Render dynamic description templates with current datetime values."""
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
        """Return fallback dynamic description templates."""
        return [
            "Updated at {time} on {weekday}",
            "Current vibe as of {date}",
            "Live update – {time}",
        ]

    def _update_summary(self, **fields) -> None:
        """Append run outcome details to the in-memory summary."""
        if not isinstance(self.last_run_summary, dict):
            self.last_run_summary = {}
        self.last_run_summary.update(fields)

    def _determine_interval_seconds(self) -> int:
        """Compute effective run interval, falling back to schedule defaults."""
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
        """Convert scheduler interval strings into integer seconds."""
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
