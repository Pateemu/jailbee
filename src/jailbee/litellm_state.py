"""Host-only state of the LiteLLM proxy.

    <xdg_data_home>/jailbee/litellm/
      ports.json                      account -> port
      <account>/master.key            0600, created once (dev containers get a copy)
      <account>/applied.sha256        digest of the files the running unit last restarted on
      <account>/applied-hot.sha256    digest of the hot.json the running unit last confirmed

Everything the proxy itself reads (rendered config, instance.env with its
secrets, ChatGPT tokens) lives in the `jailbee-litellm-state` Incus volume,
never on the host filesystem (`litellm._push_state`).
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from jailbee.config.models_litellm import ACCOUNT_NAME_RE
from jailbee.paths import xdg_data_home

BASE_PORT = 4100


def state_dir() -> Path:
    return xdg_data_home() / "jailbee" / "litellm"


def _private_dir(path: Path) -> Path:
    """Create `path` 0700 (and repair it if it already exists looser)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def check_account(account: str) -> str:
    """An account name becomes a path component, so it must not be able to leave the state dir."""
    if not ACCOUNT_NAME_RE.fullmatch(account):
        raise ValueError(
            f"invalid LiteLLM account name {account!r}: use 1-32 lowercase letters, digits, "
            "'-' or '_', starting with a letter or digit"
        )
    return account


def _account_dir(account: str) -> Path:
    return _private_dir(state_dir() / check_account(account))


def _write_private(path: Path, text: str) -> bool:
    """Write `text` 0600 atomically; return whether its bytes changed.

    Replaced through a temp file so a crash never leaves a truncated file that a
    restarting unit would then read.
    """
    payload = text.encode()
    before = path.read_bytes() if path.exists() else None
    if before == payload:
        path.chmod(0o600)
        return False
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


@contextmanager
def _state_lock() -> Iterator[None]:
    """Serialize account creation across CLI processes (and threads)."""
    base = _private_dir(state_dir())
    fd = os.open(base / ".allocation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        flock(fd, LOCK_UN)
        os.close(fd)


def _read_ports(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        ports = json.loads(path.read_text())
    except ValueError as error:
        raise RuntimeError(
            f"{path} is not valid JSON ({error}); fix or delete it, then re-run "
            "(deleting it reassigns the accounts' ports)."
        ) from error
    if not isinstance(ports, dict):
        raise RuntimeError(f"{path} must hold a JSON object of account -> port.")
    return ports


def port_for(account: str) -> int:
    check_account(account)
    path = state_dir() / "ports.json"
    with _state_lock():
        ports = _read_ports(path)
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


def known_port(account: str) -> int | None:
    """The account's port if one was ever allocated; never allocates."""
    ports = _read_ports(state_dir() / "ports.json")
    port = ports.get(check_account(account))
    return port if isinstance(port, int) else None


def master_key_path(account: str) -> Path:
    """Where the account's master key lives; creates nothing."""
    return state_dir() / check_account(account) / "master.key"


def _stamped(account: str, name: str, digest: str) -> bool:
    try:
        stamp = (state_dir() / check_account(account) / name).read_text().strip()
    except OSError:
        return False
    return stamp == digest


def config_applied(account: str, digest: str) -> bool:
    """Whether the running unit was last restarted on files with this (cold) digest.

    Files are pushed before the restart, so a run that fails in between would
    otherwise leave a stale proxy that a re-run never restarts. A missing
    stamp counts as not applied.
    """
    return _stamped(account, "applied.sha256", digest)


def hot_applied(account: str, digest: str) -> bool:
    """Whether the running unit last confirmed (or restarted on) a `hot.json` with this digest."""
    return _stamped(account, "applied-hot.sha256", digest)


def record_applied(account: str, digest: str) -> None:
    _write_private(_account_dir(account) / "applied.sha256", digest + "\n")


def record_hot_applied(account: str, digest: str) -> None:
    _write_private(_account_dir(account) / "applied-hot.sha256", digest + "\n")
