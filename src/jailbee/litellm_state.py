"""Host-side state of the LiteLLM proxy, bind-mounted into `jailbee-litellm`.

    <xdg_data_home>/jailbee/litellm/
      ports.json                      account -> port
      callback/jailbee_callback.py    copied from the installed package on every write
      <account>/master.key            0600, created once
      <account>/auth/                 0700; LiteLLM's CHATGPT_TOKEN_DIR
      <account>/config.yaml           rendered
      <account>/callback.json         rendered
      <account>/instance.env          0600, rendered (holds the master key)
      <account>/applied.sha256        digest of the files the running unit last restarted on

State survives `jailbee litellm down` and container rebuilds, so a rebuild
does not require a new login. LiteLLM writes auth.json; its file mode depends
on the proxy process umask, not this module. The auth directory is kept 0700.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from jailbee.litellm_render import (
    render_callback_data,
    render_instance_config,
    render_instance_env,
)
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig

BASE_PORT = 4100


def state_dir() -> Path:
    return xdg_data_home() / "jailbee" / "litellm"


def _account_dir(account: str) -> Path:
    path = state_dir() / account
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_private(path: Path, text: str) -> bool:
    """Write `text` 0600; return whether its bytes changed."""
    payload = text.encode()
    before = path.read_bytes() if path.exists() else None
    if before == payload:
        path.chmod(0o600)
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(payload)
    path.chmod(0o600)
    return True


@contextmanager
def _state_lock() -> Iterator[None]:
    """Serialize account creation across CLI processes (and threads)."""
    base = state_dir()
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(base / ".allocation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        flock(fd, LOCK_UN)
        os.close(fd)


def port_for(account: str) -> int:
    path = state_dir() / "ports.json"
    with _state_lock():
        ports: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
        if account not in ports:
            ports[account] = max(ports.values(), default=BASE_PORT - 1) + 1
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(json.dumps(ports, indent=2, sort_keys=True) + "\n")
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return ports[account]


def master_key(account: str) -> str:
    path = _account_dir(account) / "master.key"
    with _state_lock():
        if not path.exists():
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(f"sk-jb-{secrets.token_urlsafe(32)}\n")
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        path.chmod(0o600)
        return path.read_text().strip()


@dataclass(frozen=True)
class WriteResult:
    changed: bool


def write_instance_files(cfg: LiteLLMConfig, account: str) -> WriteResult:
    base = _account_dir(account)
    auth = base / "auth"
    auth.mkdir(exist_ok=True)
    auth.chmod(0o700)
    callback_dir = state_dir() / "callback"
    callback_dir.mkdir(exist_ok=True)
    source = (
        resources.files("jailbee.provision").joinpath("litellm").joinpath("jailbee_callback.py")
    ).read_text()
    changed = [
        _write_private(callback_dir / "jailbee_callback.py", source),
        _write_private(
            base / "config.yaml", yaml.safe_dump(render_instance_config(cfg), sort_keys=False)
        ),
        _write_private(
            base / "callback.json", json.dumps(render_callback_data(cfg), indent=2) + "\n"
        ),
        _write_private(
            base / "instance.env",
            render_instance_env(
                port=port_for(account), master_key=master_key(account), account=account
            ),
        ),
    ]
    return WriteResult(changed=any(changed))


def config_digest(account: str) -> str:
    """Digest of every file the proxy unit reads from the state directory."""
    base = state_dir() / account
    sha = hashlib.sha256()
    for path in (
        state_dir() / "callback" / "jailbee_callback.py",
        base / "config.yaml",
        base / "callback.json",
        base / "instance.env",
    ):
        sha.update(path.name.encode() + b"\0")
        sha.update(path.read_bytes() if path.exists() else b"")
    return sha.hexdigest()


def config_applied(account: str) -> bool:
    """Whether the running unit was last restarted on exactly the files now on disk.

    Files are written before the restart, so a run that fails in between would
    otherwise leave a stale proxy that a re-run, seeing unchanged files, never
    restarts. A missing stamp counts as not applied.
    """
    try:
        stamp = (state_dir() / account / "applied.sha256").read_text().strip()
    except OSError:
        return False
    return stamp == config_digest(account)


def record_applied(account: str) -> None:
    _write_private(_account_dir(account) / "applied.sha256", config_digest(account) + "\n")


def _auth_file(account: str) -> Path:
    return state_dir() / account / "auth" / "auth.json"


def auth_state(account: str) -> Literal["missing", "present"]:
    try:
        data = json.loads(_auth_file(account).read_text())
    except (OSError, ValueError):
        return "missing"
    if isinstance(data, dict) and any(
        isinstance(data.get(name), str) and data[name] for name in ("access_token", "refresh_token")
    ):
        return "present"
    return "missing"


def logout(account: str) -> bool:
    path = _auth_file(account)
    if not path.exists():
        return False
    path.unlink()
    return True
