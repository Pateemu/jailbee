"""`jailbee-litellm` lifecycle through a MagicMock Incus (style of test_registry.py)."""

import os
from importlib import resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import litellm as ll
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(ll, "_resolve_egress", lambda hosts: [])  # no DNS in tests
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)
    return tmp_path


def _gcfg(enabled: bool = True) -> GlobalConfig:
    return GlobalConfig.model_validate({"litellm": {"enabled": enabled}})


def _incus(*, present: bool, running: bool = True, installed: str | None = "1.103.0") -> MagicMock:
    incus = MagicMock()
    incus.network_get.return_value = "10.79.115.1/24"
    incus.network_exists.return_value = True
    incus.profile_exists.return_value = True
    incus.network_acl_exists.return_value = True
    incus.list_containers.return_value = (
        [{"name": ll.LITELLM_CONTAINER, "status": "Running" if running else "Stopped"}]
        if present else []
    )

    def exec_(name, cmd, **_kw):
        text = " ".join(cmd)
        if "importlib.metadata" in text:
            if installed is None:
                raise IncusError("no venv")
            return f"{installed}\n"
        if "is-active" in text:
            return "active\n"
        if "health/liveliness" in text:
            return "ok\n"
        return ""

    incus.exec.side_effect = exec_
    return incus


def _execs(incus: MagicMock) -> list[str]:
    return [" ".join(c.args[1]) for c in incus.exec.call_args_list]


def test_up_refuses_when_disabled():
    with pytest.raises(ValueError, match="litellm.enabled"):
        ll.litellm_up(_incus(present=False), _gcfg(enabled=False))


def test_up_creates_provisions_and_opens_services_rule():
    incus = _incus(present=False)
    result = ll.litellm_up(incus, _gcfg())
    incus.init.assert_called_once_with("images:ubuntu/26.04/cloud", ll.LITELLM_CONTAINER)
    assert any("/root/install.sh" in e for e in _execs(incus))
    assert result.ip == "10.79.115.3" and result.port == 4100 and result.installed is True
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    assert yaml.safe_load(services[-1].args[1])["egress"][0]["destination"] == "10.79.115.3/32"


def test_profile_attaches_egress_acl_only_after_install():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    profile_yamls = [
        c.args[1] for c in incus.profile_set_yaml.call_args_list if c.args[0] == ll.LITELLM_PROFILE
    ]
    first, last = (yaml.safe_load(p) for p in (profile_yamls[0], profile_yamls[-1]))
    assert "security.acls" not in first["devices"]["eth0"]
    assert last["devices"]["eth0"]["security.acls"] == ll.EGRESS_ACL
    assert last["devices"]["eth0"]["ipv4.address"] == "10.79.115.3"


def test_up_is_quiet_when_nothing_changed():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec.reset_mock()
    result = ll.litellm_up(incus, _gcfg())
    assert result.restarted is False
    assert not any("systemctl restart" in e for e in _execs(incus))


def test_up_restarts_once_after_config_change():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec.reset_mock()
    changed = GlobalConfig.model_validate(
        {"litellm": {"enabled": True, "routes": {"sol-xhigh": {"effort": "max"}}}}
    )
    result = ll.litellm_up(incus, changed)
    assert result.restarted is True
    assert sum("systemctl restart" in e for e in _execs(incus)) == 1


def test_version_mismatch_reinstalls():
    incus = _incus(present=True, installed="1.90.0")
    result = ll.litellm_up(incus, _gcfg())
    assert result.installed is True


def test_down_empties_services_rule_and_deletes_container():
    incus = _incus(present=True)
    ll.litellm_down(incus)
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    assert yaml.safe_load(services[-1].args[1])["egress"] == []


def test_status_missing():
    assert ll.litellm_status(_incus(present=False)).container == ll.ContainerState.MISSING


def test_status_running_reports_instance_and_login(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    status = ll.litellm_status(incus)
    assert status.container == ll.ContainerState.RUNNING
    assert status.version == "1.103.0"
    assert status.instances == [
        ll.InstanceStatus(account="default", port=4100, active=True, healthy=True, login="missing")
    ]


def test_endpoint_none_without_container():
    assert ll.endpoint(_incus(present=False)) is None


def test_endpoint_is_static_ip_and_port():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    assert ll.endpoint(incus) == ("10.79.115.3", 4100)


def test_service_limits_token_file_mode(tmp_path: Path):
    unit = resources.files("jailbee.provision").joinpath("litellm", "jailbee-litellm@.service")
    mask_lines = [line for line in unit.read_text().splitlines() if line.startswith("UMask=")]
    assert mask_lines == ["UMask=0077"]
    # Emulate a file created by the proxy under the service's declared mask.
    old_mask = os.umask(int(mask_lines[0].split("=", 1)[1], 8))
    try:
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "example"}')
        assert auth_file.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_mask)


def test_install_uses_only_hash_locked_requirements_by_default():
    script = resources.files("jailbee.provision").joinpath("litellm", "install.sh")
    assert (
        'pip install --require-hashes --no-deps -r /root/litellm-requirements.lock'
        in script.read_text()
    )


def test_unlocked_version_is_shell_quoted():
    incus = _incus(present=False)
    malicious = "1.103.0; touch /root/unwanted"
    ll._provision(incus, malicious, pinned=False)
    command = incus.exec.call_args.args[1][2]
    assert "JAILBEE_LITELLM_UNLOCKED_VERSION='1.103.0; touch /root/unwanted'" in command


def test_up_fails_closed_to_dev_containers_on_install_error():
    incus = _incus(present=False)
    incus.exec.side_effect = IncusError("apt unavailable")
    with pytest.raises(IncusError, match="apt unavailable"):
        ll.litellm_up(incus, _gcfg())
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_up_requires_static_bridge_address():
    incus = _incus(present=False)
    incus.network_get.return_value = "none"
    with pytest.raises(RuntimeError, match="static address"):
        ll.litellm_up(incus, _gcfg())
    incus.init.assert_not_called()


def test_status_stopped_does_not_probe_container():
    incus = _incus(present=True, running=False)
    assert ll.litellm_status(incus) == ll.LiteLLMStatus(
        container=ll.ContainerState.STOPPED, ip="10.79.115.3", version=None, instances=[]
    )
    incus.exec.assert_not_called()
