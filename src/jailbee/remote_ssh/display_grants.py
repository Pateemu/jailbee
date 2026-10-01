"""Forwarding grants: which SSH key may open a tunnel to the shared display.

The tunnel (`ssh -N -L ...`) is a different SSH connection from the one that
launched an app, so a grant is bound to the *key* (its fingerprint), not the
connection. A launch records one; `JailbeeSSHServer.connection_requested`
looks it up. The server and the child it starts are different processes, so
the record is a file under `state_dir()`.

A grant names one destination, always the loopback port the display's Incus
proxy device listens on; `is_allowed` compares all of key, host and port.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from jailbee.db import state_dir

GRANT_HOST = "127.0.0.1"
GRANT_MAX_AGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Grant:
    fingerprint: str
    host: str
    port: int
    container: str
    created: float


def _grants_dir() -> Path:
    return state_dir() / "ssh-display"


def _path(fingerprint: str, host: str, port: int) -> Path:
    digest = hashlib.sha256(f"{fingerprint}|{host}|{port}".encode()).hexdigest()[:32]
    return _grants_dir() / f"{digest}.json"


def record_grant(
    fingerprint: str, host: str, port: int, container: str, *, now: float | None = None
) -> Path:
    directory = _grants_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(fingerprint, host, port)
    grant = Grant(fingerprint, host, port, container, time.time() if now is None else now)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(grant)))
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def is_allowed(fingerprint: str, host: str, port: int, *, now: float | None = None) -> bool:
    path = _path(fingerprint, host, port)
    try:
        data = json.loads(path.read_text())
        grant = Grant(**data)
    except (OSError, ValueError, TypeError):
        return False
    if (grant.fingerprint, grant.host, grant.port) != (fingerprint, host, port):
        return False
    current = time.time() if now is None else now
    if current - grant.created > GRANT_MAX_AGE_SECONDS:
        path.unlink(missing_ok=True)
        return False
    return True


def clear_grants() -> None:
    directory = _grants_dir()
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        path.unlink(missing_ok=True)
