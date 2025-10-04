"""Minimal FastAPI interface for controlling the Spotifreak supervisor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..app_context import determine_paths, load_context
from ..config import ConfigError
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


class CommandResponse(BaseModel):
    status: str
    message: Optional[str] = None


@app.get("/syncs")
def list_syncs(config_dir: Optional[str] = Query(default=None)):
    context = _load_app_context(config_dir)
    syncs = []
    for sync in context.syncs:
        syncs.append(
            {
                "id": sync.id,
                "type": sync.type,
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
