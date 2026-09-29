"""Manage the dedicated LiteLLM Incus container and its per-account proxy.

Provision with a temporary package-host-only ACL and no auth state mount,
then restrict egress to providers before mounting state and starting the proxy.
Host-side authentication and configuration survive container deletion.
"""

from __future__ import annotations

import json
import os
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from jailbee import litellm_state
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import IncusError
from jailbee.litellm_render import ACCOUNT_DEFAULT, CONTAINER_STATE_DIR, egress_hosts
from jailbee.loose_bridge import LOOSE_BRIDGE, loose_bridge_host_ip
from jailbee.network import service_container_acl_yaml
from jailbee.services_acl import set_services_endpoint

if TYPE_CHECKING:
    from jailbee.egress import EgressEntry
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

LITELLM_CONTAINER = "jailbee-litellm"
LITELLM_PROFILE = "jailbee-litellm-profile"
EGRESS_ACL = "jailbee-litellm-egress"
UNIT = "jailbee-litellm@{account}.service"
_IMAGE = "images:ubuntu/26.04/cloud"
_IP_INDEX = 1
_WAIT_SECONDS = 60
_PY = "/opt/litellm/bin/python"
_PACKAGE_ENDPOINTS = (
    "pypi.org:443", "files.pythonhosted.org:443",
    "archive.ubuntu.com:80", "archive.ubuntu.com:443",
    "security.ubuntu.com:80", "security.ubuntu.com:443",
    "ports.ubuntu.com:80", "ports.ubuntu.com:443",
)
CONTAINER_FILE = "/etc/jailbee/litellm.json"
CONTAINER_KEY_FILE = "/etc/jailbee/litellm-default.key"


def unit(account: str) -> str:
    return UNIT.format(account=account)


def _no_steps(_message: str) -> None:
    """Default callback for progress updates."""


class ContainerState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"


@dataclass(frozen=True)
class InstanceStatus:
    account: str
    port: int
    active: bool
    healthy: bool
    login: Literal["missing", "present"]


@dataclass(frozen=True)
class LiteLLMStatus:
    container: ContainerState
    ip: str | None
    version: str | None
    instances: list[InstanceStatus]


@dataclass(frozen=True)
class UpResult:
    ip: str
    port: int
    restarted: bool
    installed: bool


def _resolve_egress(hosts: list[str]) -> list[EgressEntry]:
    from jailbee.egress import build_egress_entries

    return build_egress_entries([h if ":" in h else f"{h}:443" for h in hosts])


def _set_egress(incus: Incus, entries: list[EgressEntry], port: int) -> None:
    if not incus.network_acl_exists(EGRESS_ACL):
        incus.network_acl_create(EGRESS_ACL)
    incus.network_acl_set_yaml(
        EGRESS_ACL, service_container_acl_yaml(EGRESS_ACL, entries, listen_ports=[port])
    )


def _check_static_ip(incus: Incus, ip: str, containers: list[dict[str, object]]) -> None:
    """Fail before changing the instance when another NIC/lease owns our fixed IP."""
    own = next((c for c in containers if c.get("name") == LITELLM_CONTAINER), {})
    config = own.get("config")
    own_mac = config.get("volatile.eth0.hwaddr") if isinstance(config, dict) else None
    for lease in incus.network_leases(LOOSE_BRIDGE):
        if lease.get("address") == ip and not (
            lease.get("hostname") == LITELLM_CONTAINER
            or (own_mac and lease.get("hwaddr") == own_mac)
        ):
            owner = lease.get("hostname") or "an unknown DHCP client"
            raise RuntimeError(
                f"LiteLLM address {ip} is leased to {owner} on {LOOSE_BRIDGE}; "
                "release that lease or move the conflicting container, then run `jb litellm up`."
            )
    for container in containers:
        name = container.get("name")
        if not isinstance(name, str) or name == LITELLM_CONTAINER:
            continue
        parsed = yaml.safe_load(incus.config_show(name, expanded=True)) or {}
        devices = parsed.get("devices", {})
        if isinstance(devices, dict) and any(
            isinstance(device, dict)
            and device.get("type") == "nic"
            and device.get("network") == LOOSE_BRIDGE
            and device.get("ipv4.address") == ip
            for device in devices.values()
        ):
            raise RuntimeError(
                f"LiteLLM address {ip} is assigned to {name} on {LOOSE_BRIDGE}; "
                "change that NIC's static address, then run `jb litellm up`."
            )


def _detach_state(incus: Incus) -> None:
    """Do not suppress device-removal errors: a retained auth mount is unsafe."""
    parsed = yaml.safe_load(incus.config_show(LITELLM_CONTAINER)) or {}
    devices = parsed.get("devices", {})
    if isinstance(devices, dict) and "state" in devices:
        incus.config_device_remove(LITELLM_CONTAINER, "state")


def _profile_yaml(ip: str | None, *, with_acl: bool) -> str:
    eth0: dict[str, str] = {"type": "nic", "name": "eth0", "network": LOOSE_BRIDGE}
    if ip is not None:
        eth0["ipv4.address"] = ip
    if with_acl:
        eth0["security.acls"] = EGRESS_ACL
        eth0["security.acls.default.egress.action"] = "reject"
        eth0["security.acls.default.ingress.action"] = "reject"
    profile = {
        "name": LITELLM_PROFILE,
        "description": "security + network for the jailbee-litellm container",
        "config": {
            "security.nesting": "true",
            "raw.idmap": f"uid {os.getuid()} 0\ngid {os.getgid()} 0",
        },
        "devices": {"eth0": eth0},
    }
    return yaml.safe_dump(profile, sort_keys=False)


def _set_profile(incus: Incus, ip: str | None, *, with_acl: bool) -> None:
    if not incus.profile_exists(LITELLM_PROFILE):
        incus.profile_create(LITELLM_PROFILE)
    incus.profile_set_yaml(LITELLM_PROFILE, _profile_yaml(ip, with_acl=with_acl))


def _container(incus: Incus) -> dict[str, object] | None:
    for container in incus.list_containers():
        if container.get("name") == LITELLM_CONTAINER:
            return container
    return None


def _installed_version(incus: Incus) -> str | None:
    probe = "import importlib.metadata as m; print(m.version('litellm'))"
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=30).strip() or None
    except IncusError:
        return None


def _read(name: str) -> str:
    return resources.files("jailbee.provision").joinpath("litellm").joinpath(name).read_text()


def _provision(incus: Incus, version: str, pinned: bool) -> None:
    # Shell-quote the user-configurable version; do not interpolate raw YAML
    # into a command run as root inside the service container.
    unlocked = shlex.quote("" if pinned else version)
    script = f"""\
set -euo pipefail
cat > /root/install.sh <<'JB_INSTALL_EOF'
{_read("install.sh").rstrip()}
JB_INSTALL_EOF
cat > /root/jailbee-litellm@.service <<'JB_SERVICE_EOF'
{_read("jailbee-litellm@.service").rstrip()}
JB_SERVICE_EOF
cat > /root/litellm-requirements.lock <<'JB_LOCK_EOF'
{_read("requirements.lock").rstrip()}
JB_LOCK_EOF
chmod +x /root/install.sh
JAILBEE_LITELLM_UNLOCKED_VERSION={unlocked} /root/install.sh
"""
    incus.exec_with_input(LITELLM_CONTAINER, ["bash", "-s"], script, timeout=900)


def _annotate_recovery(error: BaseException, details: list[str]) -> None:
    recovery = "; ".join(details)
    error.add_note(recovery)
    # The CLI prints str(IncusError), not traceback notes. Keep the original
    # exception object/traceback and make any failed recovery visible.
    if isinstance(error, IncusError):
        error.args = (f"{error}; {recovery}",)


def _stop_unrestricted_container(incus: Incus, details: list[str]) -> None:
    """Disable autostart and stop; delete if either protection fails."""
    disabled = stopped = True
    try:
        incus.config_set(LITELLM_CONTAINER, "boot.autostart", "false")
    except Exception as disable_error:
        disabled = False
        details.append(f"Failed to disable LiteLLM autostart: {disable_error}")
    try:
        incus.stop(LITELLM_CONTAINER, force=True)
        details.append("container force-stopped")
    except Exception as stop_error:
        stopped = False
        details.append(f"Failed to force-stop unrestricted LiteLLM container: {stop_error}")
    if disabled and stopped:
        return
    try:
        incus.delete(LITELLM_CONTAINER, force=True)
        details.append("container force-deleted")
    except Exception as delete_error:
        details.append(
            "SECURITY: LiteLLM container may still run or autostart without an egress ACL; "
            f"force-delete failed: {delete_error}"
        )


def _secure_failed_create(incus: Incus, error: BaseException) -> None:
    """Discard a partial instance even if `init`/`start` reported an error."""
    try:
        incus.delete(LITELLM_CONTAINER, force=True)
    except Exception as delete_error:
        details = [f"Failed to delete partial LiteLLM container: {delete_error}"]
        _stop_unrestricted_container(incus, details)
        _annotate_recovery(error, details)


def _secure_failed_install(incus: Incus, ip: str, error: BaseException) -> None:
    """Reattach default-deny egress or retire the unprotected container.

    Do not resolve providers on this error path: DNS can also be broken during
    provisioning, so a DHCP/DNS-only ACL is safer and reliably renderable.
    Keep the original install exception and annotate any cleanup failure.
    """
    try:
        if not incus.network_acl_exists(EGRESS_ACL):
            incus.network_acl_create(EGRESS_ACL)
        incus.network_acl_set_yaml(
            EGRESS_ACL, service_container_acl_yaml(EGRESS_ACL, [], listen_ports=[])
        )
        _set_profile(incus, ip, with_acl=True)
    except Exception as restore_error:
        details = [f"Failed to restore restrictive LiteLLM NIC ACL: {restore_error}"]
        _stop_unrestricted_container(incus, details)
        _annotate_recovery(error, details)


def _active(incus: Incus, account: str) -> bool:
    try:
        return (
            incus.exec(
                LITELLM_CONTAINER, ["systemctl", "is-active", unit(account)], timeout=10
            ).strip()
            == "active"
        )
    except IncusError:
        return False


def _healthy(incus: Incus, port: int) -> bool:
    probe = (
        "import urllib.request; "
        f"urllib.request.urlopen('http://127.0.0.1:{port}/health/liveliness', timeout=5); "
        "print('ok')"
    )
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=15).strip() == "ok"
    except IncusError:
        return False


def _wait_healthy(incus: Incus, account: str, port: int, on_step: Callable[[str], None]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        if _active(incus, account) and _healthy(incus, port):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{unit(account)} did not become healthy within {_WAIT_SECONDS}s. "
                "See `jailbee litellm logs`."
            )
        on_step(f"waiting for {unit(account)}")
        time.sleep(2)


def litellm_up(
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    reinstall: bool = False,
    on_step: Callable[[str], None] = _no_steps,
) -> UpResult:
    cfg = gcfg.litellm
    if not cfg.enabled:
        raise ValueError("LiteLLM is disabled: set `litellm.enabled: true` in global.yaml first.")
    account = ACCOUNT_DEFAULT
    version = cfg.effective_version()
    pinned = cfg.version is None

    on_step("rendering the proxy configuration")
    written = litellm_state.write_instance_files(cfg, account)
    port = litellm_state.port_for(account)

    if not incus.network_exists(LOOSE_BRIDGE):
        incus.network_create(LOOSE_BRIDGE)
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if ip is None:
        raise RuntimeError(
            f"{LOOSE_BRIDGE} has no concrete IPv4 subnet; the LiteLLM proxy needs a static address."
        )

    containers = incus.list_containers()
    _check_static_ip(incus, ip, containers)
    info = next((c for c in containers if c.get("name") == LITELLM_CONTAINER), None)
    needs_install = reinstall or info is None
    if info is None:
        _set_egress(incus, _resolve_egress(list(_PACKAGE_ENDPOINTS)), port)
        _set_profile(incus, ip, with_acl=True)
        on_step(f"creating {LITELLM_CONTAINER} from {_IMAGE}")
        try:
            incus.init(_IMAGE, LITELLM_CONTAINER)
            incus.profile_assign(LITELLM_CONTAINER, ["default", LITELLM_PROFILE])
            incus.start(LITELLM_CONTAINER)
        except BaseException as error:
            _secure_failed_create(incus, error)
            raise
    elif info.get("status") != "Running" and not reinstall:
        # A stopped container can retain an ACL-free profile from an interrupted
        # install. Restrict it before start: Incus may report a failed start
        # after the instance has already reached Running.
        try:
            if not incus.network_acl_exists(EGRESS_ACL):
                incus.network_acl_create(EGRESS_ACL)
            incus.network_acl_set_yaml(
                EGRESS_ACL, service_container_acl_yaml(EGRESS_ACL, [], listen_ports=[])
            )
            _set_profile(incus, ip, with_acl=True)
            incus.start(LITELLM_CONTAINER)
        except BaseException as error:
            _secure_failed_install(incus, ip, error)
            raise
    if not needs_install and _installed_version(incus) != version:
        needs_install = True

    try:
        if needs_install:
            if info is not None:
                # No live service or mounted token directory may see package egress.
                incus.config_set(LITELLM_CONTAINER, "boot.autostart", "false")
                if info.get("status") == "Running" or not reinstall:
                    incus.stop(LITELLM_CONTAINER, force=True)
                _detach_state(incus)
                _set_egress(incus, _resolve_egress(list(_PACKAGE_ENDPOINTS)), port)
                _set_profile(incus, ip, with_acl=True)
                incus.start(LITELLM_CONTAINER)
            on_step(f"installing LiteLLM {version} (up to 15 min)")
            _provision(incus, version, pinned)

        on_step("writing the proxy's egress allowlist")
        acl_yaml = service_container_acl_yaml(
            EGRESS_ACL, _resolve_egress(egress_hosts(cfg)), listen_ports=[port]
        )
        if not incus.network_acl_exists(EGRESS_ACL):
            incus.network_acl_create(EGRESS_ACL)
        incus.network_acl_set_yaml(EGRESS_ACL, acl_yaml)
        _set_profile(incus, ip, with_acl=True)
        if needs_install:
            incus.config_device_add(
                LITELLM_CONTAINER, "state", "disk",
                {"source": str(litellm_state.state_dir()), "path": CONTAINER_STATE_DIR},
            )
    except BaseException as error:
        if needs_install:
            _secure_failed_install(incus, ip, error)
        raise

    if needs_install:
        # Never autoboot while package access and auth state can coexist.
        incus.config_set(LITELLM_CONTAINER, "boot.autostart", "true")

    restart = needs_install or written.changed or not _active(incus, account)
    if restart:
        incus.exec(LITELLM_CONTAINER, ["systemctl", "enable", unit(account)], timeout=30)
        incus.exec(LITELLM_CONTAINER, ["systemctl", "restart", unit(account)], timeout=60)
    _wait_healthy(incus, account, port, on_step)

    set_services_endpoint(incus, (ip, [port]))
    return UpResult(ip=ip, port=port, restarted=restart, installed=needs_install)


def litellm_down(incus: Incus) -> None:
    set_services_endpoint(incus, None)
    if _container(incus) is not None:
        incus.delete(LITELLM_CONTAINER, force=True)


def endpoint(incus: Incus) -> tuple[str, int] | None:
    if _container(incus) is None:
        return None
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if ip is None:
        return None
    return ip, litellm_state.port_for(ACCOUNT_DEFAULT)


def container_sync_payload(incus: Incus, gcfg: GlobalConfig) -> dict[str, object] | None:
    """Resolve the dev-container settings without putting a key in a background job."""
    from jailbee.litellm_render import container_payload

    if not gcfg.litellm.enabled:
        return None
    ep = endpoint(incus)
    key_path = litellm_state.state_dir() / ACCOUNT_DEFAULT / "master.key"
    if ep is None or not key_path.exists():
        return None
    ip, port = ep
    return {
        "json": container_payload(
            gcfg.litellm, base_url=f"http://{ip}:{port}", key_file=CONTAINER_KEY_FILE
        ),
        "key_path": str(key_path),
    }


def sync_container(incus: Incus, name: str, payload: dict[str, object] | None) -> None:
    """Install both files in a running dev container, or retire stale settings."""
    if payload is None:
        script = f"rm -f {CONTAINER_FILE} {CONTAINER_KEY_FILE}"
        incus.exec(name, ["bash", "-c", script], timeout=30)
        return

    body = json.dumps(payload["json"], indent=2)
    key = Path(str(payload["key_path"])).read_text().strip()
    assert "'" not in key
    script = f"""\
set -euo pipefail
mkdir -p /etc/jailbee
tmp=$(mktemp)
printf '%s\\n' '{key}' > "$tmp"
chmod 0640 "$tmp"; chown root:{CONTAINER_USERNAME} "$tmp"; mv "$tmp" {CONTAINER_KEY_FILE}
tmp=$(mktemp)
cat > "$tmp" <<'JB_EOF'
{body}
JB_EOF
chmod 0644 "$tmp"; mv "$tmp" {CONTAINER_FILE}
"""
    incus.exec_with_input(name, ["bash", "-s"], script, timeout=30)


def upstream_reachable(incus: Incus, host: str) -> bool:
    """Probe provider TCP reachability from inside the restricted proxy."""
    probe = f"import socket; socket.create_connection(({host!r}, 443), 5); print('ok')"
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=15).strip() == "ok"
    except IncusError:
        return False


def litellm_status(incus: Incus) -> LiteLLMStatus:
    info = _container(incus)
    if info is None:
        return LiteLLMStatus(ContainerState.MISSING, None, None, [])
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if info.get("status") != "Running":
        return LiteLLMStatus(ContainerState.STOPPED, ip, None, [])
    port = litellm_state.port_for(ACCOUNT_DEFAULT)
    instance = InstanceStatus(
        account=ACCOUNT_DEFAULT,
        port=port,
        active=_active(incus, ACCOUNT_DEFAULT),
        healthy=_healthy(incus, port),
        login=litellm_state.auth_state(ACCOUNT_DEFAULT),
    )
    return LiteLLMStatus(ContainerState.RUNNING, ip, _installed_version(incus), [instance])


def _require_running(incus: Incus) -> None:
    info = _container(incus)
    if info is None or info.get("status") != "Running":
        raise RuntimeError(f"{LITELLM_CONTAINER} is not running. Run `jailbee litellm up` first.")


def litellm_login(incus: Incus, account: str) -> int:
    """Start LiteLLM's own ChatGPT device-code flow on an interactive PTY."""
    _require_running(incus)
    env_file = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/instance.env")
    auth_dir = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/auth")
    script = (
        f"set -e; umask 0077; test -r {env_file}; unset CHATGPT_TOKEN_DIR; "
        f"set -a; . {env_file}; set +a; "
        f'test "${{CHATGPT_TOKEN_DIR:-}}" = {auth_dir}; '
        f"exec {_PY} -c 'from litellm.llms.chatgpt.authenticator import Authenticator; "
        f'Authenticator().get_access_token(); print("Logged in.")\''
    )
    return incus.exec_interactive(LITELLM_CONTAINER, ["bash", "-c", script])


def litellm_logs(incus: Incus, account: str, *, follow: bool, lines: int = 200) -> int:
    """Display the per-account systemd journal, optionally following updates."""
    _require_running(incus)
    cmd = ["journalctl", "-u", unit(account), "-n", str(lines), "--no-pager"]
    if follow:
        cmd.append("-f")
    return incus.exec_interactive(LITELLM_CONTAINER, cmd)
