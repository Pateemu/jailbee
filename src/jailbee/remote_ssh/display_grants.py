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
import math
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from jailbee.db import state_dir

GRANT_HOST = "127.0.0.1"
GRANT_MAX_AGE_SECONDS = 24 * 3600
_CLOCK_SKEW_SECONDS = 60.0


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
    directory.chmod(0o700)
    path = _path(fingerprint, host, port)
    grant = Grant(fingerprint, host, port, container, time.time() if now is None else now)
    temporary = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(asdict(grant)))
    temporary.replace(path)
    return path


def _parse(data: object) -> Grant | None:
    """A well-typed record, or `None`: anything else is simply no grant."""
    if not isinstance(data, dict):
        return None
    fingerprint = data.get("fingerprint")
    host = data.get("host")
    port = data.get("port")
    container = data.get("container")
    created = data.get("created")
    if not (
        isinstance(fingerprint, str)
        and isinstance(host, str)
        and isinstance(container, str)
        and isinstance(port, int)
        and not isinstance(port, bool)
        and isinstance(created, int | float)
        and not isinstance(created, bool)
        and math.isfinite(created)
    ):
        return None
    return Grant(fingerprint, host, port, container, float(created))


def is_allowed(fingerprint: str, host: str, port: int, *, now: float | None = None) -> bool:
    path = _path(fingerprint, host, port)
    try:
        grant = _parse(json.loads(path.read_text()))
    except (OSError, ValueError):
        return False
    if grant is None or (grant.fingerprint, grant.host, grant.port) != (fingerprint, host, port):
        return False
    current = time.time() if now is None else now
    if grant.created > current + _CLOCK_SKEW_SECONDS:
        return False
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
