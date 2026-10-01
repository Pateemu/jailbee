"""The shared display container, the client wait and the connection recipe."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import remote_display as rd
from jailbee.incus import IncusError
from jailbee.remote_ssh import display_grants


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _running(name=rd.DISPLAY_CONTAINER):
    return [{"name": name, "status": "Running"}]


def test_profile_uses_the_clients_idmap_and_the_loose_bridge():
    profile = yaml.safe_load(rd._display_profile_yaml(1234, 5678))

    assert profile["config"]["raw.idmap"] == "uid 1234 1234\ngid 5678 5678"
    assert profile["devices"]["eth0"]["network"] == "jailbee-loose"


def test_status_missing_stopped_running_degraded():
    incus = MagicMock()
    incus.list_containers.return_value = []
    assert rd.display_status(incus) is rd.DisplayStatus.MISSING

    incus.list_containers.return_value = [{"name": rd.DISPLAY_CONTAINER, "status": "Stopped"}]
    assert rd.display_status(incus) is rd.DisplayStatus.STOPPED

    incus.list_containers.return_value = _running()
    incus.exec.return_value = "active\n"
    assert rd.display_status(incus) is rd.DisplayStatus.RUNNING

    incus.exec.side_effect = IncusError("inactive")
    assert rd.display_status(incus) is rd.DisplayStatus.DEGRADED


def test_up_creates_provisions_and_publishes_the_port():
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    incus.init.assert_called_once()
    assert incus.init.call_args.args[1] == rd.DISPLAY_CONTAINER
    devices = {c.args[1]: c.args[3] for c in incus.config_device_add.call_args_list}
    assert devices["shared"]["path"] == "/run/jailbee-display"
    assert devices["rdp"] == {
        "listen": "tcp:127.0.0.1:13389",
        "connect": "tcp:127.0.0.1:3389",
    }
    incus.start.assert_called_with(rd.DISPLAY_CONTAINER)


def test_up_creates_the_host_directory_privately(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    directory = tmp_path / "jailbee" / "display"
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700


def test_provisioning_script_carries_both_files_and_the_identity():
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.exec.return_value = "active\n"

    rd.display_up(incus, sleep_fn=lambda _s: None)

    scripts = [c.args[1][2] for c in incus.exec.call_args_list if c.args[1][:2] == ["bash", "-c"]]
    provisioning = next(s for s in scripts if "JAILBEE_INSTALL_EOF" in s)
    assert "--address=127.0.0.1" in provisioning
    assert "--shell=desktop" in provisioning
    assert "JAILBEE_UID=" in provisioning


def test_down_stops_and_revokes_every_grant(mocker):
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    stop = mocker.patch("jailbee.remote_display.stop_container")
    display_grants.record_grant("SHA256:a", "127.0.0.1", 13389, "feat-1")

    rd.display_down(incus)

    stop.assert_called_once()
    assert display_grants.is_allowed("SHA256:a", "127.0.0.1", 13389) is False


def test_client_connected_reads_established_connections():
    incus = MagicMock()
    incus.exec.return_value = "1\n"
    assert rd.client_connected(incus) is True

    incus.exec.return_value = "0\n"
    assert rd.client_connected(incus) is False

    incus.exec.side_effect = IncusError("boom")
    assert rd.client_connected(incus) is False


def test_wait_for_client_polls_until_connected():
    incus = MagicMock()
    incus.exec.side_effect = ["0\n", "0\n", "1\n"]
    sleeps = []

    assert rd.wait_for_client(incus, timeout_s=10, poll_s=1, sleep_fn=sleeps.append) is True
    assert sleeps == [1, 1]


def test_wait_for_client_gives_up():
    incus = MagicMock()
    incus.exec.return_value = "0\n"

    assert rd.wait_for_client(incus, timeout_s=3, poll_s=1, sleep_fn=lambda _s: None) is False


def test_the_recipe_is_two_steps_with_a_host_placeholder():
    lines = rd.format_connection_info(rd.connection_info(8022))
    text = "\n".join(lines)

    assert "ssh -N -L 3389:127.0.0.1:13389 -p 8022 jailbee@<host>" in text
    assert "localhost:3389" in text


def test_ensure_display_mount_tolerates_an_existing_device():
    incus = MagicMock()
    incus.config_device_add.side_effect = IncusError("Device already exists")

    rd.ensure_display_mount(incus, "feat-1")  # must not raise


def test_prepare_records_a_grant_and_returns_when_a_client_is_connected():
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    incus.exec.side_effect = ["active\n", "1\n"]  # service state, then client count
    said = []

    rd.prepare_shared_display(
        incus,
        "feat-1",
        fingerprint="SHA256:a",
        ssh_port=8022,
        say=said.append,
        sleep_fn=lambda _s: None,
    )

    assert display_grants.is_allowed("SHA256:a", "127.0.0.1", 13389) is True
    incus.config_device_add.assert_called()  # the display mount
    assert said == []  # nothing to explain: the client is already there


def test_prepare_without_a_client_prints_the_recipe_waits_and_fails():
    """Review focus 3."""
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    incus.exec.side_effect = lambda *a, **k: "active\n" if "systemctl" in a[1] else "0\n"
    said = []

    with pytest.raises(rd.DisplayError, match="RDP client"):
        rd.prepare_shared_display(
            incus,
            "feat-1",
            fingerprint="SHA256:a",
            ssh_port=8022,
            say=said.append,
            sleep_fn=lambda _s: None,
            wait_seconds=3,
        )

    assert any("ssh -N -L" in line for line in said)


def test_prepare_refuses_a_session_with_no_known_key():
    incus = MagicMock()
    incus.list_containers.return_value = _running()
    incus.exec.return_value = "active\n"

    with pytest.raises(rd.DisplayError, match="SSH key"):
        rd.prepare_shared_display(
            incus,
            "feat-1",
            fingerprint=None,
            ssh_port=8022,
            say=lambda _l: None,
            sleep_fn=lambda _s: None,
        )
