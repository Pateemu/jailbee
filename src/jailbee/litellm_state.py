"""Host-side state of the LiteLLM proxy, bind-mounted into `jailbee-litellm`.

    <xdg_data_home>/jailbee/litellm/
      ports.json                      account -> port
      callback/jailbee_callback.py    copied from the installed package on every write
      <account>/master.key            0600, created once
      <account>/auth/                 0700; LiteLLM's CHATGPT_TOKEN_DIR
      <account>/config.yaml           rendered
      <account>/callback.json         rendered
      <account>/instance.env          0600, rendered (holds the master key)

State survives `jailbee litellm down` and container rebuilds, so a rebuild
does not require a new login. LiteLLM writes auth.json; its file mode depends
on the proxy process umask, not this module. The auth directory is kept 0700.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
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


def port_for(account: str) -> int:
    path = state_dir() / "ports.json"
    state_dir().mkdir(parents=True, exist_ok=True)
    ports: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    if account not in ports:
        ports[account] = max(ports.values(), default=BASE_PORT - 1) + 1
        path.write_text(json.dumps(ports, indent=2, sort_keys=True) + "\n")
    return ports[account]


def master_key(account: str) -> str:
    path = _account_dir(account) / "master.key"
    if not path.exists():
        _write_private(path, f"sk-jb-{secrets.token_urlsafe(32)}\n")
    else:
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
