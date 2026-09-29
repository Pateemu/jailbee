"""`jailbee-litellm` lifecycle through a MagicMock Incus (style of test_registry.py)."""

import os
import subprocess
from importlib import resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import litellm as ll
from jailbee.egress import EgressEntry
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        ll,
        "_resolve_egress",
        lambda hosts: [
            EgressEntry(
                destinations=["192.0.2.1"],
                port=int(h.rsplit(":", 1)[1]) if ":" in h else 443,
                description=h if ":" in h else f"{h}:443",
            )
            for h in hosts
        ],
    )  # no DNS in tests
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
    incus.config_show.return_value = yaml.safe_dump(
        {"devices": {"state": {"type": "disk", "path": ll.CONTAINER_STATE_DIR}}}
    )
    incus.list_containers.return_value = (
        [{"name": ll.LITELLM_CONTAINER, "status": "Running" if running else "Stopped"}]
        if present
        else []
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
    with pytest.raises(ValueError, match=r"litellm\.enabled"):
        ll.litellm_up(_incus(present=False), _gcfg(enabled=False))


def test_up_creates_provisions_and_opens_services_rule():
    incus = _incus(present=False)
    result = ll.litellm_up(incus, _gcfg())
    incus.init.assert_called_once_with("images:ubuntu/26.04/cloud", ll.LITELLM_CONTAINER)
    assert incus.exec_with_input.call_args.args[1] == ["bash", "-s"]
    assert "/root/install.sh" in incus.exec_with_input.call_args.args[2]
    assert result.ip == "10.79.115.3" and result.port == 4100 and result.installed is True
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    assert yaml.safe_load(services[-1].args[1])["egress"][0]["destination"] == "10.79.115.3/32"


def test_install_is_package_restricted_and_auth_mount_is_added_after_install():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    calls = incus.mock_calls
    install = next(i for i, c in enumerate(calls) if c[0] == "exec_with_input")
    mount = next(i for i, c in enumerate(calls) if c[0] == "config_device_add")
    assert install < mount
    before = [c for c in calls[:install] if c[0] == "network_acl_set_yaml"]
    assert before
    assert {
        r.get("description", "").removeprefix("allowlisted: ")
        for r in yaml.safe_load(before[-1].args[1])["egress"]
    } >= {"pypi.org:443", "files.pythonhosted.org:443", "archive.ubuntu.com:80"}
    assert all(
        "security.acls" in yaml.safe_load(c.args[1])["devices"]["eth0"]
        for c in calls
        if c[0] == "profile_set_yaml"
    )
    final = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert final["ipv4.address"] == "10.79.115.3"


def test_provision_streams_real_lock_at_subprocess_boundary(mocker):
    from jailbee.incus import Incus

    run = mocker.patch("jailbee.incus.subprocess.run")
    run.return_value = subprocess.CompletedProcess([], 0, "", "")
    ll._provision(Incus(), "1.103.0", True)
    args, kwargs = run.call_args
    assert args[0][-2:] == ["bash", "-s"]
    assert len(kwargs["input"]) > 200_000
    assert "litellm==1.103.0" in kwargs["input"]
    assert all(len(arg) < 4096 for arg in args[0])


def test_provision_failure_does_not_echo_install_script_in_error(mocker):
    from jailbee.incus import Incus

    mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, "", "apt failed"),
    )
    with pytest.raises(IncusError) as caught:
        ll._provision(Incus(), "1.103.0", True)
    assert "apt failed" in str(caught.value)
    assert "litellm==1.103.0" not in str(caught.value)


def test_reinstall_detaches_auth_before_package_egress_and_reattaches_after():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(), reinstall=True)
    calls = incus.mock_calls
    stopped = next(i for i, c in enumerate(calls) if c[0] == "stop")
    removed = next(i for i, c in enumerate(calls) if c[0] == "config_device_remove")
    package = next(i for i, c in enumerate(calls) if c[0] == "network_acl_set_yaml")
    install = next(i for i, c in enumerate(calls) if c[0] == "exec_with_input")
    mount = next(i for i, c in enumerate(calls) if c[0] == "config_device_add")
    assert stopped < removed < package < install < mount
    assert calls[removed].kwargs == {}
    assert any(
        c[0] == "config_set" and c.args[1:] == ("boot.autostart", "false") for c in calls[:package]
    )


def test_install_failure_never_reattaches_auth_or_opens_services():
    incus = _incus(present=True)
    incus.exec_with_input.side_effect = IncusError("apt unavailable")
    with pytest.raises(IncusError, match="apt unavailable"):
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    incus.config_device_add.assert_not_called()
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)
    final = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert all(r.get("destination_port") in {"67", "547", "53"} for r in final["egress"])


def test_reinstall_aborts_before_package_egress_if_state_cannot_be_detached():
    incus = _incus(present=True)
    incus.config_device_remove.side_effect = IncusError("state device busy")
    with pytest.raises(IncusError, match="state device busy"):
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    incus.exec_with_input.assert_not_called()
    incus.config_device_add.assert_not_called()
    # Recovery uses DHCP/DNS-only egress, never a package-host ACL.
    assert all(
        r.get("destination_port") in {"67", "547", "53"}
        for c in incus.network_acl_set_yaml.call_args_list
        for r in yaml.safe_load(c.args[1])["egress"]
    )


def test_bridge_lease_collision_rejected_before_instance_changes():
    incus = _incus(present=False)
    incus.network_leases.return_value = [{"address": "10.79.115.3", "hostname": "other"}]
    with pytest.raises(RuntimeError, match=r"10\.79\.115\.3.*other.*jailbee-loose"):
        ll.litellm_up(incus, _gcfg())
    incus.init.assert_not_called()


def test_bridge_static_nic_collision_rejected_even_without_lease():
    incus = _incus(present=False)
    incus.list_containers.return_value = [{"name": "other", "status": "Stopped"}]
    incus.config_show.return_value = yaml.safe_dump(
        {
            "devices": {
                "eth0": {"type": "nic", "network": "jailbee-loose", "ipv4.address": "10.79.115.3"}
            }
        }
    )
    with pytest.raises(RuntimeError, match=r"10\.79\.115\.3.*other"):
        ll.litellm_up(incus, _gcfg())


def test_bridge_self_lease_and_static_nic_are_allowed():
    incus = _incus(present=True)
    incus.network_leases.return_value = [
        {"address": "10.79.115.3", "hostname": ll.LITELLM_CONTAINER}
    ]
    incus.config_show.return_value = yaml.safe_dump(
        {
            "devices": {
                "eth0": {"type": "nic", "network": "jailbee-loose", "ipv4.address": "10.79.115.3"}
            }
        }
    )
    assert ll.litellm_up(incus, _gcfg()).ip == "10.79.115.3"


def test_bridge_self_lease_can_be_identified_by_nic_mac_without_hostname():
    incus = _incus(present=True)
    incus.list_containers.return_value = [
        {
            "name": ll.LITELLM_CONTAINER,
            "status": "Running",
            "config": {"volatile.eth0.hwaddr": "00:16:3e:01:02:03"},
        }
    ]
    incus.network_leases.return_value = [
        {"address": "10.79.115.3", "hostname": "", "hwaddr": "00:16:3e:01:02:03"}
    ]
    assert ll.litellm_up(incus, _gcfg()).ip == "10.79.115.3"


def test_sync_publishes_json_only_after_private_key():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    ll.sync_container(incus, "repo-branch", ll.container_sync_payload(incus, _gcfg()))
    script = incus.exec_with_input.call_args.args[2]
    assert script.index('mv "$tmp" ' + ll.CONTAINER_KEY_FILE) < script.index(
        'mv "$tmp" ' + ll.CONTAINER_FILE
    )


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
        "pip install --require-hashes --no-deps -r /root/litellm-requirements.lock"
        in script.read_text()
    )
    assert "pip install --upgrade pip" not in script.read_text()


def test_unlocked_version_is_shell_quoted():
    incus = _incus(present=False)
    malicious = "1.103.0; touch /root/unwanted"
    ll._provision(incus, malicious, pinned=False)
    command = incus.exec_with_input.call_args.args[2]
    assert "JAILBEE_LITELLM_UNLOCKED_VERSION='1.103.0; touch /root/unwanted'" in command


def test_up_fails_closed_to_dev_containers_on_install_error():
    incus = _incus(present=False)
    incus.exec.side_effect = IncusError("apt unavailable")
    with pytest.raises(IncusError, match="apt unavailable"):
        ll.litellm_up(incus, _gcfg())
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


@pytest.mark.parametrize(
    "operation",
    ["init", "profile_assign", "start"],
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
        idx
        for idx, call in enumerate(incus.mock_calls)
        if call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
    ]
    autostart = [
        idx
        for idx, call in enumerate(incus.mock_calls)
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
    incus.exec_with_input.side_effect = install_error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=reinstall)
    assert caught.value is install_error
    acl_calls = [
        call for call in incus.network_acl_set_yaml.call_args_list if call.args[0] == ll.EGRESS_ACL
    ]
    assert len(acl_calls) >= 2  # package ACL, then DHCP/DNS-only recovery
    acl = yaml.safe_load(acl_calls[-1].args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert profile["security.acls.default.egress.action"] == "reject"
    assert profile["security.acls.default.ingress.action"] == "reject"
    assert all(
        call.args[0] != "jailbee-services" for call in incus.network_acl_set_yaml.call_args_list
    )
    if present:
        incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    else:
        incus.stop.assert_not_called()


def test_failed_acl_restore_force_stops_container_without_masking_install_error():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    assert any("ACL edit unavailable" in note for note in caught.value.__notes__)
    assert "ACL edit unavailable" in str(caught.value)
    assert "force-stopped" in str(caught.value)
    assert incus.stop.call_count >= 1
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")


def test_failed_acl_restore_deletes_if_autostart_cannot_be_disabled():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.config_set.side_effect = [None, IncusError("autostart disable unavailable")]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_deletes_container_if_force_stop_fails():
    incus = _incus(present=False)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.stop.side_effect = IncusError("stop unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_reports_if_even_delete_fails():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.stop.side_effect = [None, IncusError("stop unavailable")]
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

    resolver = ll._resolve_egress

    def unavailable(hosts):
        if hosts == ll.egress_hosts(_gcfg().litellm):
            raise OSError("DNS unavailable")
        return resolver(hosts)

    monkeypatch.setattr(ll, "_resolve_egress", unavailable)
    with pytest.raises(OSError, match="DNS unavailable"):
        ll.litellm_up(incus, _gcfg())
    acl = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    assert all("destination" in rule for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_every_proxy_acl_write_pins_dns_and_dhcp_to_the_bridge():
    """Install-time and final ACLs alike: no destination-less infrastructure rule."""
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    writes = [
        yaml.safe_load(c.args[1])
        for c in incus.network_acl_set_yaml.call_args_list
        if c.args[0] == ll.EGRESS_ACL
    ]
    assert len(writes) >= 2
    for acl in writes:
        infra = [r for r in acl["egress"] if not r["description"].startswith("allowlisted: ")]
        assert infra
        assert all("destination" in r for r in infra)
        dns = [r for r in infra if r["destination_port"] == "53"]
        assert dns and all(r["destination"].startswith("10.79.115.1/32") for r in dns)


def test_failed_acl_write_after_install_retries_restrictive_acl():
    incus = _incus(present=False)
    error = IncusError("ACL write unavailable")
    incus.network_acl_set_yaml.side_effect = [None, error, None]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    assert incus.network_acl_set_yaml.call_count == 3
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
    env_file.write_text("CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/default/auth\n")
    prefix = script.split("exec ", 1)[0].replace(
        "/var/lib/jailbee-litellm/default/instance.env", str(env_file)
    )
    auth_file = tmp_path / "auth.json"
    subprocess.run(
        ["bash", "-c", prefix + f'python -c \'open("{auth_file}", "w").write("token")\''],
        check=True,
    )
    assert auth_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "contents",
    [None, "", "CHATGPT_TOKEN_DIR=/tmp/unmounted/auth\n", "false\n"],
    ids=["missing", "empty-despite-inherited-env", "wrong-token-directory", "invalid-script"],
)
def test_login_does_not_invoke_authenticator_if_env_file_is_invalid(
    tmp_path: Path, contents: str | None
):
    incus = _incus(present=True)
    ll.litellm_login(incus, "default")
    command = incus.exec_interactive.call_args.args[1][-1]
    env_file = tmp_path / "instance.env"
    if contents is not None:
        env_file.write_text(contents)
    marker = tmp_path / "authenticator-invoked"
    # Execute the emitted shell command with Authenticator replaced by a
    # harmless marker. No real Incus or authentication subprocess is started.
    script = command.replace("/var/lib/jailbee-litellm/default/instance.env", str(env_file))
    script = script.split("exec ", 1)[0] + f"exec touch {marker}"
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "CHATGPT_TOKEN_DIR": "/var/lib/jailbee-litellm/default/auth",
        },
    )
    assert result.returncode != 0
    assert not marker.exists()


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
        i
        for i, call in enumerate(incus.mock_calls)
        if call[0] == "network_acl_set_yaml" and call.args[0] == ll.EGRESS_ACL
    )
    restricted_at = next(
        i
        for i, call in enumerate(incus.mock_calls)
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


def test_sync_payload_none_when_disabled_absent_or_without_key():
    assert ll.container_sync_payload(_incus(present=True), _gcfg(enabled=False)) is None
    assert ll.container_sync_payload(_incus(present=False), _gcfg()) is None
    assert ll.container_sync_payload(_incus(present=True), _gcfg()) is None


def test_sync_payload_after_up():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    payload = ll.container_sync_payload(incus, _gcfg())
    assert payload is not None
    assert payload["json"]["profiles"]["codex"]["base_url"] == "http://10.79.115.3:4100"
    assert payload["json"]["profiles"]["codex"]["key_file"] == ll.CONTAINER_KEY_FILE
    assert str(payload["key_path"]).endswith("default/master.key")
    assert "sk-jb-" not in repr(payload)


def test_sync_container_writes_json_and_key():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    payload = ll.container_sync_payload(incus, _gcfg())
    incus.exec_with_input.reset_mock()
    ll.sync_container(incus, "repo-branch", payload)
    name, cmd, script = incus.exec_with_input.call_args.args
    assert name == "repo-branch"
    assert cmd == ["bash", "-s"]
    assert "sk-jb-" not in repr(cmd)
    assert ll.CONTAINER_FILE in script and ll.CONTAINER_KEY_FILE in script
    assert "chmod 0644" in script
    assert "chmod 0640" in script and "chown root:dev" in script
    assert "sk-jb-" in script


def test_sync_container_removes_when_none():
    incus = _incus(present=True)
    ll.sync_container(incus, "repo-branch", None)
    script = incus.exec.call_args.args[1][-1]
    assert f"rm -f {ll.CONTAINER_FILE} {ll.CONTAINER_KEY_FILE}" in script
    incus.exec_with_input.assert_not_called()


def test_upstream_reachable_checks_proxy_container_only():
    incus = _incus(present=True)
    incus.exec.side_effect = None
    incus.exec.return_value = "ok\n"
    assert ll.upstream_reachable(incus, "chatgpt.com")
    assert incus.exec.call_args.args[0] == ll.LITELLM_CONTAINER
    assert "chatgpt.com" in incus.exec.call_args.args[1][-1]
    assert "443" in incus.exec.call_args.args[1][-1]
    incus.exec.side_effect = IncusError("blocked")
    assert not ll.upstream_reachable(incus, "chatgpt.com")
