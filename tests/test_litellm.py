"""`jailbee-litellm` lifecycle through a MagicMock Incus (style of test_registry.py)."""

import os
import subprocess
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


@pytest.mark.parametrize(
    "operation",
    ["init", "profile_assign", "config_device_add", "start"],
)
def test_new_container_setup_failure_deletes_possible_unrestricted_instance(operation: str):
    incus = _incus(present=False)
    startup_error = IncusError(f"{operation} reported failure")
    getattr(incus, operation).side_effect = startup_error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    # `init`/`start` can report an error after taking effect. Never infer
    # absence from the error or an out-of-date container listing.
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_ambiguous_start_failure_does_not_leave_autostarting_container():
    incus = _incus(present=False)
    start_error = IncusError("start timed out after instance reached Running")

    def start_then_fail(_name):
        incus.list_containers.return_value = [{"name": ll.LITELLM_CONTAINER, "status": "Running"}]
        raise start_error

    def delete_instance(_name, *, force):
        assert force
        incus.list_containers.return_value = []

    incus.start.side_effect = start_then_fail
    incus.delete.side_effect = delete_instance
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is start_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert incus.list_containers() == []


def test_new_container_autostart_is_enabled_only_after_acl_is_attached():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    restricted = [
        idx for idx, call in enumerate(incus.mock_calls)
        if call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
    ]
    autostart = [
        idx for idx, call in enumerate(incus.mock_calls)
        if call[0] == "config_set" and call.args[1:] == ("boot.autostart", "true")
    ]
    assert len(autostart) == 1
    assert restricted and restricted[-1] < autostart[0]


def test_ambiguous_autostart_failure_leaves_restrictive_acl_attached():
    incus = _incus(present=False)
    error = IncusError("config_set timed out after enabling autostart")
    incus.config_set.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert profile["security.acls.default.egress.action"] == "reject"


def test_failed_new_container_delete_disables_autostart_before_force_stop():
    incus = _incus(present=False)
    startup_error = IncusError("start timed out")
    incus.start.side_effect = startup_error
    incus.delete.side_effect = IncusError("delete failed")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")
    incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert "delete failed" in str(caught.value)


def test_failed_new_container_cleanup_reports_unrestricted_instance():
    incus = _incus(present=False)
    startup_error = IncusError("start timed out")
    incus.start.side_effect = startup_error
    incus.delete.side_effect = IncusError("delete failed")
    incus.stop.side_effect = IncusError("stop failed")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    assert "SECURITY" in str(caught.value)
    assert "stop failed" in str(caught.value)


@pytest.mark.parametrize("present,reinstall", [(False, False), (True, True)])
def test_failed_install_restores_restrictive_nic_acl(present: bool, reinstall: bool):
    incus = _incus(present=present)
    incus.network_acl_exists.return_value = False
    install_error = IncusError("apt unavailable")
    ordinary_exec = incus.exec.side_effect

    def failing_install(name, cmd, **kwargs):
        if "/root/install.sh" in " ".join(cmd):
            raise install_error
        return ordinary_exec(name, cmd, **kwargs)

    incus.exec.side_effect = failing_install
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=reinstall)
    assert caught.value is install_error
    acl_calls = [
        call for call in incus.network_acl_set_yaml.call_args_list
        if call.args[0] == ll.EGRESS_ACL
    ]
    assert len(acl_calls) == 1
    acl = yaml.safe_load(acl_calls[0].args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert profile["security.acls.default.egress.action"] == "reject"
    assert profile["security.acls.default.ingress.action"] == "reject"
    assert all(
        call.args[0] != "jailbee-services" for call in incus.network_acl_set_yaml.call_args_list
    )
    incus.stop.assert_not_called()


def test_failed_acl_restore_force_stops_container_without_masking_install_error():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = IncusError("ACL edit unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    assert any("ACL edit unavailable" in note for note in caught.value.__notes__)
    assert "ACL edit unavailable" in str(caught.value)
    assert "force-stopped" in str(caught.value)
    incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")


def test_failed_acl_restore_deletes_if_autostart_cannot_be_disabled():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = IncusError("ACL edit unavailable")
    incus.config_set.side_effect = IncusError("autostart disable unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_deletes_container_if_force_stop_fails():
    incus = _incus(present=False)
    install_error = IncusError("apt unavailable")
    incus.exec.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = IncusError("ACL edit unavailable")
    incus.stop.side_effect = IncusError("stop unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_reports_if_even_delete_fails():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = IncusError("ACL edit unavailable")
    incus.stop.side_effect = IncusError("stop unavailable")
    incus.delete.side_effect = IncusError("delete unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    assert "stop unavailable" in str(caught.value.__notes__)
    assert "delete unavailable" in str(caught.value.__notes__)
    assert "SECURITY" in str(caught.value)
    assert "delete unavailable" in str(caught.value)


def test_failed_acl_resolution_after_install_restricts_container(monkeypatch: pytest.MonkeyPatch):
    incus = _incus(present=False)

    def unavailable(_hosts):
        raise OSError("DNS unavailable")

    monkeypatch.setattr(ll, "_resolve_egress", unavailable)
    with pytest.raises(OSError, match="DNS unavailable"):
        ll.litellm_up(incus, _gcfg())
    acl = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_failed_acl_write_after_install_retries_restrictive_acl():
    incus = _incus(present=False)
    error = IncusError("ACL write unavailable")
    incus.network_acl_set_yaml.side_effect = [error, None]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    assert incus.network_acl_set_yaml.call_count == 2
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL


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


def test_login_runs_litellm_device_flow_with_private_umask(tmp_path: Path):
    incus = _incus(present=True)
    incus.exec_interactive.return_value = 0
    assert ll.litellm_login(incus, "default") == 0
    name, cmd = incus.exec_interactive.call_args.args
    script = cmd[-1]
    assert name == ll.LITELLM_CONTAINER
    assert cmd[:2] == ["bash", "-c"]
    assert ". /var/lib/jailbee-litellm/default/instance.env" in script
    assert "Authenticator().get_access_token()" in script
    # Run the login shell prefix with a harmless stand-in for Authenticator.
    # A newly created auth.json in the bind-mounted host directory must be 0600.
    env_file = tmp_path / "instance.env"
    env_file.write_text("CHATGPT_TOKEN_DIR=/unused\n")
    prefix = script.split("exec ", 1)[0].replace(
        "/var/lib/jailbee-litellm/default/instance.env", str(env_file)
    )
    auth_file = tmp_path / "auth.json"
    subprocess.run(
        ["bash", "-c", prefix + f"python -c 'open(\"{auth_file}\", \"w\").write(\"token\")'"],
        check=True,
    )
    assert auth_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("present,running", [(False, True), (True, False)])
def test_login_requires_running_container(present: bool, running: bool):
    incus = _incus(present=present, running=running)
    with pytest.raises(RuntimeError, match="jailbee litellm up"):
        ll.litellm_login(incus, "default")
    incus.exec_interactive.assert_not_called()


def test_logs_follow_flag():
    incus = _incus(present=True)
    incus.exec_interactive.return_value = 0
    assert ll.litellm_logs(incus, "default", follow=True) == 0
    assert incus.exec_interactive.call_args.args == (
        ll.LITELLM_CONTAINER,
        ["journalctl", "-u", "jailbee-litellm@default.service", "-n", "200", "--no-pager", "-f"],
    )


def test_stopped_container_gets_restrictive_acl_before_start():
    incus = _incus(present=True, running=False)
    ll.litellm_up(incus, _gcfg())
    start_at = next(i for i, call in enumerate(incus.mock_calls) if call[0] == "start")
    acl_at = next(
        i for i, call in enumerate(incus.mock_calls)
        if call[0] == "network_acl_set_yaml" and call.args[0] == ll.EGRESS_ACL
    )
    restricted_at = next(
        i for i, call in enumerate(incus.mock_calls)
        if call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
    )
    assert acl_at < restricted_at < start_at
    incus.delete.assert_not_called()


def test_stopped_container_ambiguous_start_failure_restricts_before_start():
    incus = _incus(present=True, running=False)
    error = IncusError("start timed out after container became Running")
    incus.start.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    start_at = next(i for i, call in enumerate(incus.mock_calls) if call[0] == "start")
    assert any(
        call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
        for call in incus.mock_calls[:start_at]
    )


def test_stopped_container_failed_acl_restore_force_stops_before_start():
    incus = _incus(present=True, running=False)
    error = IncusError("ACL setup failed")
    incus.network_acl_set_yaml.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    incus.start.assert_not_called()
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")
    incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
