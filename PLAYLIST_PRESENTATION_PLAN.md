# Playlist Presentation Flexibility Plan

## Goals
- Support "maximum flexibility" for playlist presentation syncs without per-user code edits.
- Allow asset rotation sourced from directories as well as explicit lists.
- Provide fine-grained cadence control per feature (cover, title, description).
- Offer robust selection strategies (sequential, random, weighted) with pluggable rules.
- Maintain backwards compatibility with existing configuration files where possible.

## Key Scenarios
1. **Random rotation from a folder every minute**
   - User specifies a directory containing cover images.
   - Module randomly selects a new asset every 60 seconds without repeating back-to-back.
2. **Sequential rotation through assets every 10 seconds**
   - User wants deterministic cycling through a folder (or list) of assets with a short cadence.
3. **Mixed feature cadence**
   - Covers change every run, titles change every third run, descriptions change once per phase.
4. **Weighted asset selection**
   - Certain hero images should appear more frequently than others.
5. **Phase-specific behavior**
   - Morning uses a photo directory, evening uses a curated list, night falls back to a default image.
6. **Dynamic descriptions with fallbacks**
   - Combine directory-driven titles with dynamic, weighted description templates.

## Proposed Configuration Schema Changes
### Top-Level Options
- `interval_seconds`: override cadence (>=1s) independent of schedule.
- `respect_schedule`: boolean (default `true`). When `false`, ignore the sync schedule and rely solely on `interval_seconds`.
- `random_seed`: optional string to produce deterministic random sequences per sync.

### Feature Asset Sources
Allow each feature (`cover`, `title`, `description`) to specify a list of sources. Each source can be one of:

```yaml
sources:
  - type: list            # explicit items
    items:
      - path/to/image1.jpg
      - path/to/image2.jpg
  - type: folder          # load all files in a folder
    path: assets/covers/night
    pattern: "*.jpg"      # glob (optional)
    recursive: false
    weight: 2             # optional weight for weighted selection
    shuffle_on_load: true # shuffle when enumerated
  - type: fallback        # only used if above sources resolve empty
    items:
      - default.jpg
```

Additional source-level fields:
- `cache_ttl_seconds`: how long to cache directory listings (default 300s).
- `max_items`: optional cap on items from this source.

### Selection Strategy
```yaml
selection:
  mode: sequential | random | weighted_random | round_robin
  dedupe_window: 1          # prevent reuse within last N selections
  restart_policy: loop | bounce | random_restart
  group_key: cover-title    # tie features together (e.g., cover/title share index)
```
- `weighted_random` respects `weight` from sources and optional per-item weights.
- `round_robin` cycles across sources before moving to next item within each.

### Cadence Multipliers
```yaml
cadence:
  multiplier: 1             # per-feature multiplier of base interval
  phase_overrides:
    morning: 120            # seconds
    night: 600
```
- When `multiplier` is >1, the feature updates every nth run.
- `phase_overrides` can change cadence dynamically when `_determine_phase` is in effect.

### Feature Overrides (per cover/title/description)
- `enabled`: boolean (default `false`).
- `sources`: array of source definitions (see above).
- `fallback_asset`: asset used when sources resolve empty or fail to load.
- `failure_mode`: `skip | reuse_last | stop` (controls behavior on upload/update errors).
- `dynamic_templates`: existing but add:
  - `selection`: same options as assets.
  - `weight`: per template weighting.
  - `fallback`: text when templates produce empty output.

### State & History
- `state.keep_history`: number of past selections to persist (per feature & phase).
- `state.reset_on_phase_change`: boolean (default `false`). When `true`, sequential order restarts when phase changes.

### Validation Enhancements
- `validate_on_start`: whether to check all asset sources (existence, size limits) before first run.
- `allowed_extensions`: optional list to restrict to certain file types.

## Implementation Notes
1. **Schema updates**
   - Extend `FeatureOptions` into a richer structure (may need new dataclasses/pydantic models).
   - Provide migration path for legacy configs (`assets: { default: [] }`).

2. **Asset enumeration**
   - Implement folder listing helper with caching and pattern filtering (use `glob` or `pathlib.rglob`).
   - Combine sources into a flattened, weighted pool per phase/feature.

3. **Selection engine**
   - Abstract selection into a strategy class with sequential/random/weighted behaviors, dedupe support, and history persistence.
   - Use shared state store to maintain indices, dedupe windows, and random seeds per feature/phase.

4. **Cadence handling**
   - Adjust `_determine_interval_seconds` to apply multipliers and phase overrides.
   - Track run counts in state to know when a feature should update (`cadence.multiplier`).

5. **Feature grouping**
   - When `selection.group_key` is set across features, store shared cursor references so cover/title can stay in sync.

6. **Error handling**
   - Wrap asset fetch/encode in retries with `failure_mode` fallbacks.
   - Populate run summary with structured details (selected asset, source type, skipped reason).

7. **Testing**
   - Add unit tests covering new schema, directory sourcing, selection strategies, and cadence logic.
   - Include integration tests using temporary directories with dummy files.

## Implementation Checklist
- [x] Extend schema to support `sources`, folder ingestion, selection strategies, cadence, grouping, fallbacks.
- [x] Add runtime helpers for directory enumeration, caching, and selection strategies (sequential, random, weighted, round-robin).
- [x] Support per-feature cadence multipliers and phase-specific overrides.
- [x] Introduce feature grouping via `selection.group_key` so covers/titles/descriptions stay in sync.
- [x] Preserve backwards compatibility with legacy `assets` lists.
- [ ] Update README and sample configs to showcase the new options.
- [ ] Add automated tests covering the new behaviours.

## Open Questions
- Do we need per-feature rate limiting to avoid frequent cover uploads hitting Spotify API limits?
- Should we support remote assets (HTTP URLs) or only local files?
- How to expose these options in the CLI/UI without overwhelming users (maybe presets/templates)?
- Should asset enumeration happen at supervisor start or lazily per run?
