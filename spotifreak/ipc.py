"""IPC helpers for communicating with the running supervisor."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Dict


class IPCError(RuntimeError):
    """Raised when communication with the supervisor fails."""


def send_ipc_command(socket_path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send ``payload`` to the supervisor IPC socket and return the response."""

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            client.sendall(json.dumps(payload).encode("utf-8"))
            data = client.recv(65536)
    except FileNotFoundError as exc:  # pragma: no cover - depends on runtime
        raise IPCError("Supervisor IPC socket not found. Is 'spotifreak serve' running?") from exc
    except (socket.timeout, ConnectionRefusedError) as exc:  # pragma: no cover - runtime errors
        raise IPCError("Unable to communicate with supervisor.") from exc

    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - invalid JSON
        raise IPCError("Received invalid response from supervisor.") from exc
