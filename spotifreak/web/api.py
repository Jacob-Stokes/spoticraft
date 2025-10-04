"""Minimal FastAPI interface for controlling the Spotifreak supervisor."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
import mimetypes

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..app_context import determine_paths, load_context
from ..config import (
    ConfigError,
    SyncConfig,
    TemplateDefinition,
    DEFAULT_SYNC_EXTENSION,
    DEFAULT_TEMPLATE_EXTENSION,
    delete_sync_config,
    delete_template_config,
    iter_asset_entries,
    iter_sync_config_paths,
    iter_template_config_paths,
    load_builtin_templates,
    load_sync_config_file,
    load_template_file,
    sync_config_path,
    template_config_path,
    write_sync_config,
    write_template_config,
)
from ..ipc import send_ipc_command, IPCError
from ..state import load_state, state_path_for_sync

app = FastAPI(title="Spotifreak API", version="0.1.0")

STATIC_DIR = Path(__file__).with_name("static")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def ui_root():
        return FileResponse(STATIC_DIR / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def ui_missing():
        raise HTTPException(status_code=404, detail="UI assets are not available")


class SyncCommand(BaseModel):
    command: str


class CommandResponse(BaseModel):
    status: str
    message: Optional[str] = None


class SyncFileSummary(BaseModel):
    id: str
    filename: str
    path: str
    type: Optional[str] = None
    description: Optional[str] = None
    schedule: Optional[dict[str, Any]] = None
    options: Optional[dict[str, Any]] = None
    valid: bool = True
    error: Optional[str] = None
    modified_at: Optional[str] = None


class SyncFileDetail(SyncFileSummary):
    content: str
    parsed: Optional[dict[str, Any]] = None


class SyncFileContent(BaseModel):
    content: str


class TemplateSummary(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source: str
    filename: Optional[str] = None
    path: Optional[str] = None
    content: Optional[str] = None
    valid: bool = True
    error: Optional[str] = None
    modified_at: Optional[str] = None


class TemplatePayload(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    content: str


class AssetSummary(BaseModel):
    name: str
    path: str
    size_bytes: int
    modified_at: Optional[str] = None
    mime_type: Optional[str] = None
    url: Optional[str] = None
    is_dir: bool = False


def _resolve_config_dir(override: Optional[str]) -> Optional[Path]:
    if override:
        return Path(override).expanduser()
    env_value = os.getenv("SPOTIFREAK_CONFIG_DIR")
    if env_value:
        return Path(env_value).expanduser()
    return None


def _load_app_context(config_dir: Optional[str]):
    try:
        resolved = _resolve_config_dir(config_dir)
        paths = determine_paths(resolved)
        return load_context(paths)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ipc_socket_path(context) -> Path:
    return Path(context.global_config.supervisor.ipc_socket).expanduser()


@app.get("/syncs")
def list_syncs(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    syncs = []
    for sync in context.syncs:
        syncs.append(
            {
                "id": sync.id,
                "type": sync.type,
                "description": sync.description,
                "schedule": sync.schedule.model_dump(),
                "options": sync.options,
            }
        )
    return {"syncs": syncs}


@app.get("/status")
def supervisor_status(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    socket_path = _ipc_socket_path(context)
    try:
        response = send_ipc_command(socket_path, {"command": "status"})
    except IPCError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if response.get("status") != "ok":
        raise HTTPException(status_code=500, detail=response.get("message"))
    return response


def _run_supervisor_command(command: str, sync_id: str, config_dir: Optional[str]):
    context = _load_app_context(config_dir)
    if sync_id not in {sync.id for sync in context.syncs}:
        raise HTTPException(status_code=404, detail=f"Unknown sync: {sync_id}")

    socket_path = _ipc_socket_path(context)
    try:
        response = send_ipc_command(socket_path, {"command": command, "sync_id": sync_id})
    except IPCError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if response.get("status") != "ok":
        raise HTTPException(status_code=500, detail=response.get("message"))
    return CommandResponse(status="ok", message=response.get("message"))


@app.post("/syncs/{sync_id}/command", response_model=CommandResponse)
def sync_command(sync_id: str, body: SyncCommand, config_dir: Optional[str] = Query(default=None)):
    command = body.command.lower()
    if command not in {"start", "pause", "resume", "delete"}:
        raise HTTPException(status_code=400, detail="Unsupported command")
    return _run_supervisor_command(command, sync_id, config_dir)


@app.get("/syncs/{sync_id}/history")
def sync_history(sync_id: str, tail: int = Query(default=10, ge=1), config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    try:
        sync_config = next(sync for sync in context.syncs if sync.id == sync_id)
    except StopIteration:
        raise HTTPException(status_code=404, detail=f"Unknown sync: {sync_id}")

    state_path = state_path_for_sync(context.paths, context.global_config, sync_config)
    state = load_state(state_path)
    history = state.data.get("run_history", [])
    if not history:
        return {"history": []}
    return {"history": history[-tail:]}


def _modified_time(path: Path) -> Optional[str]:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _summary_from_sync(path: Path, sync: SyncConfig) -> SyncFileSummary:
    return SyncFileSummary(
        id=sync.id,
        filename=path.name,
        path=str(path),
        type=sync.type,
        description=sync.description,
        schedule=sync.schedule.model_dump(exclude_none=True),
        options=sync.options,
        valid=True,
        error=None,
        modified_at=_modified_time(path),
    )


def _summary_from_error(path: Path, error: str, fallback_id: Optional[str] = None) -> SyncFileSummary:
    return SyncFileSummary(
        id=fallback_id or path.stem,
        filename=path.name,
        path=str(path),
        valid=False,
        error=error,
        modified_at=_modified_time(path),
    )


def _detail_from_content(path: Path, content: str, *, parsed: Optional[SyncConfig], error: Optional[str]) -> SyncFileDetail:
    summary = (
        _summary_from_sync(path, parsed)
        if parsed is not None
        else _summary_from_error(path, error or "Invalid sync configuration", None)
    )
    parsed_payload = parsed.model_dump(exclude_none=True) if parsed else None
    return SyncFileDetail(**summary.model_dump(), content=content, parsed=parsed_payload)


def _serialize_template(
    template: TemplateDefinition,
    *,
    source: str,
    path: Optional[Path] = None,
    filename: Optional[str] = None,
    include_content: bool = True,
) -> TemplateSummary:
    return TemplateSummary(
        id=template.id,
        name=template.name,
        description=template.description,
        source=source,
        filename=filename,
        path=str(path) if path else None,
        content=template.content if include_content else None,
        modified_at=_modified_time(path) if path else None,
    )


def _template_error_summary(path: Path, error: str) -> TemplateSummary:
    return TemplateSummary(
        id=path.stem,
        name=path.stem,
        description=None,
        source="user",
        filename=path.name,
        path=str(path),
        content=None,
        valid=False,
        error=error,
        modified_at=_modified_time(path),
    )


def _sanitize_asset_path(raw_path: str) -> Path:
    if raw_path is None:
        raise HTTPException(status_code=400, detail="Asset path is required")
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise HTTPException(status_code=400, detail="Asset path must be relative")
    cleaned_parts = [part for part in candidate.parts if part not in {"", "."}]
    if not cleaned_parts:
        raise HTTPException(status_code=400, detail="Asset path cannot be empty")
    if any(part == ".." for part in cleaned_parts):
        raise HTTPException(status_code=400, detail="Asset path cannot contain '..'")
    return Path(*cleaned_parts)


def _asset_metadata(root: Path, path: Path) -> AssetSummary:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Asset not found: {path.name}") from None

    relative_path = path.relative_to(root)
    is_dir = path.is_dir()
    mime_type, _ = mimetypes.guess_type(path.name)
    return AssetSummary(
        name=path.name,
        path=str(relative_path).replace(os.sep, "/"),
        size_bytes=0 if is_dir else stat_result.st_size,
        modified_at=_modified_time(path),
        mime_type=mime_type if not is_dir else None,
        url=(f"/config/assets/{relative_path}".replace(os.sep, "/") if not is_dir else None),
        is_dir=is_dir,
    )


def _unique_asset_path(directory: Path, filename: str) -> Path:
    base_name = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = directory / filename
    counter = 1
    while candidate.exists():
        candidate = directory / f"{base_name}-{counter}{suffix}"
        counter += 1
    return candidate


def _ensure_allowed_asset(file: UploadFile) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file is missing a filename")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_ASSET_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_ASSET_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {allowed}")
    return ext


def _parse_yaml_content(content: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - user input
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Sync definition must be a YAML mapping")
    return data


def _validate_sync_payload(payload: dict[str, Any]) -> SyncConfig:
    try:
        return SyncConfig.model_validate(payload)
    except Exception as exc:  # pragma: no cover - user input
        raise HTTPException(status_code=400, detail=f"Invalid sync definition: {exc}") from exc


def _read_file_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Sync configuration not found: {path}") from None


@app.get("/config/syncs", response_model=dict[str, list[SyncFileSummary]])
def list_sync_files(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    summaries: list[SyncFileSummary] = []
    for path in iter_sync_config_paths(context.paths.syncs_dir):
        try:
            sync = load_sync_config_file(path)
        except ConfigError as exc:
            fallback_id = None
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("id"):
                    fallback_id = str(payload.get("id"))
            except Exception:  # pragma: no cover - defensive
                fallback_id = None
            summaries.append(_summary_from_error(path, str(exc), fallback_id))
            continue

        summaries.append(_summary_from_sync(path, sync))

    return {"syncs": summaries}


@app.get("/config/syncs/{sync_id}", response_model=SyncFileDetail)
def get_sync_file(sync_id: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    path = sync_config_path(context.paths, sync_id, must_exist=True)
    content = _read_file_content(path)
    try:
        sync = load_sync_config_file(path)
        return _detail_from_content(path, content, parsed=sync, error=None)
    except ConfigError as exc:
        return _detail_from_content(path, content, parsed=None, error=str(exc))


@app.post("/config/syncs", response_model=SyncFileDetail, status_code=201)
def create_sync_file(body: SyncFileContent, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    payload = _parse_yaml_content(body.content)
    sync = _validate_sync_payload(payload)

    path = sync_config_path(context.paths, sync.id)
    if path.exists():
        raise HTTPException(status_code=409, detail=f"Sync '{sync.id}' already exists")

    write_sync_config(path, sync)
    stored_content = _read_file_content(path)
    return _detail_from_content(path, stored_content, parsed=sync, error=None)


@app.put("/config/syncs/{sync_id}", response_model=SyncFileDetail)
def update_sync_file(sync_id: str, body: SyncFileContent, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    path = sync_config_path(context.paths, sync_id, must_exist=True)
    payload = _parse_yaml_content(body.content)
    sync = _validate_sync_payload(payload)

    if sync.id != sync_id:
        raise HTTPException(status_code=400, detail="Sync id in payload does not match path")

    write_sync_config(path, sync)
    stored_content = _read_file_content(path)
    return _detail_from_content(path, stored_content, parsed=sync, error=None)


@app.delete("/config/syncs/{sync_id}", status_code=204)
def delete_sync_file(sync_id: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    path = sync_config_path(context.paths, sync_id, must_exist=True)
    try:
        delete_sync_config(path)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return None


@app.get("/config/assets", response_model=dict[str, list[AssetSummary]])
def list_assets(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    assets = [
        _asset_metadata(context.paths.assets_dir, path)
        for path in iter_asset_entries(context.paths.assets_dir)
    ]
    assets.sort(key=lambda item: (not item.is_dir, item.path.lower()))
    return {"assets": assets}


@app.get("/config/assets/{asset_name}")
def get_asset(asset_name: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    relative_path = _sanitize_asset_path(asset_name)
    path = context.paths.assets_dir / relative_path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Asset not found: {asset_name}")
    mime_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=mime_type or "application/octet-stream", filename=path.name)


@app.post("/config/assets", response_model=AssetSummary, status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    target_dir: Optional[str] = Query(default=None, description="Optional subdirectory under assets"),
    config_dir: Optional[str] = Query(default=None),
):
    context = _load_app_context(config_dir)
    _ensure_allowed_asset(file)

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(contents) > MAX_ASSET_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds maximum allowed size (8 MB)")

    if target_dir:
        relative_dir = _sanitize_asset_path(target_dir)
        destination_dir = context.paths.assets_dir / relative_dir
    else:
        destination_dir = context.paths.assets_dir

    destination_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file.filename or "asset").name
    target_path = _unique_asset_path(destination_dir, safe_name)

    try:
        with target_path.open("wb") as handle:
            handle.write(contents)
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(status_code=500, detail=f"Failed to save asset: {exc}") from exc

    return _asset_metadata(context.paths.assets_dir, target_path)


@app.delete("/config/assets/{asset_name}", status_code=204)
def delete_asset(asset_name: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    relative_path = _sanitize_asset_path(asset_name)
    path = context.paths.assets_dir / relative_path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Asset not found: {asset_name}")
    if path.is_dir():
        raise HTTPException(status_code=400, detail="Directory deletion is not supported")
    try:
        path.unlink()
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(status_code=500, detail=f"Failed to delete asset: {exc}") from exc
    return None


def _load_templates(context) -> list[TemplateSummary]:
    summaries: list[TemplateSummary] = []

    builtin_index = {template.id: template for template in load_builtin_templates()}
    for template in builtin_index.values():
        summaries.append(
            _serialize_template(
                template,
                source="builtin",
                filename=f"{template.id}{DEFAULT_TEMPLATE_EXTENSION}",
            )
        )

    for path in iter_template_config_paths(context.paths.templates_dir):
        try:
            template = load_template_file(path)
        except ConfigError as exc:
            summaries.append(_template_error_summary(path, str(exc)))
            continue

        summaries.append(
            _serialize_template(template, source="user", path=path, filename=path.name)
        )

    return summaries


def _get_builtin_template(template_id: str) -> Optional[TemplateDefinition]:
    for template in load_builtin_templates():
        if template.id == template_id:
            return template
    return None


@app.get("/config/templates", response_model=dict[str, list[TemplateSummary]])
def list_templates(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    templates = _load_templates(context)
    return {"templates": templates}


@app.get("/config/templates/{template_id}", response_model=TemplateSummary)
def get_template(template_id: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    try:
        path = template_config_path(context.paths, template_id, must_exist=True)
    except ConfigError:
        builtin = _get_builtin_template(template_id)
        if builtin is None:
            raise HTTPException(status_code=404, detail=f"Unknown template: {template_id}")
        return _serialize_template(
            builtin,
            source="builtin",
            filename=f"{builtin.id}{DEFAULT_TEMPLATE_EXTENSION}",
        )

    template = load_template_file(path)
    return _serialize_template(template, source="user", path=path, filename=path.name)


@app.post("/config/templates", response_model=TemplateSummary, status_code=201)
def create_template(body: TemplatePayload, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)

    if _get_builtin_template(body.id):
        raise HTTPException(status_code=409, detail="Template id conflicts with built-in template")

    try:
        path = template_config_path(context.paths, body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if path.exists():
        raise HTTPException(status_code=409, detail=f"Template '{body.id}' already exists")

    template = TemplateDefinition.model_validate(body.model_dump())
    write_template_config(path, template)
    return _serialize_template(template, source="user", path=path, filename=path.name)


@app.put("/config/templates/{template_id}", response_model=TemplateSummary)
def update_template(
    template_id: str,
    body: TemplatePayload,
    config_dir: Optional[str] = Query(default=None),
):
    context = _load_app_context(config_dir)

    if _get_builtin_template(template_id):
        raise HTTPException(status_code=400, detail="Built-in templates cannot be modified")

    try:
        path = template_config_path(context.paths, template_id, must_exist=True)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.id != template_id:
        raise HTTPException(status_code=400, detail="Template id in payload does not match path")

    template = TemplateDefinition.model_validate(body.model_dump())
    write_template_config(path, template)
    return _serialize_template(template, source="user", path=path, filename=path.name)


@app.delete("/config/templates/{template_id}", status_code=204)
def delete_template(template_id: str, config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)

    if _get_builtin_template(template_id):
        raise HTTPException(status_code=400, detail="Built-in templates cannot be deleted")

    try:
        path = template_config_path(context.paths, template_id, must_exist=True)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    delete_template_config(path)
    return None


@app.post("/config/syncs/validate", response_model=SyncFileDetail)
def validate_sync_file(body: SyncFileContent):
    payload = _parse_yaml_content(body.content)
    sync = _validate_sync_payload(payload)

    dummy_path = Path(f"{sync.id}{DEFAULT_SYNC_EXTENSION}")
    summary = _summary_from_sync(dummy_path, sync)
    summary.modified_at = None
    detail = SyncFileDetail(
        **summary.model_dump(),
        content=body.content,
        parsed=sync.model_dump(exclude_none=True),
    )
    return detail
ALLOWED_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_ASSET_SIZE = 8 * 1024 * 1024  # 8 MB
