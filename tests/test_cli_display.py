from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.remote_display import DisplayError, DisplayStatus


@pytest.fixture
def display(mocker):
    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    mocker.patch("jailbee.incus.Incus")
    return mocker


def test_up_starts_the_display_and_prints_the_recipe(display):
    up = display.patch("jailbee.remote_display.display_up")

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is False
    assert callable(up.call_args.kwargs["on_step"])
    assert "ssh -N -L" in result.output


def test_up_recreate_is_passed_through(display):
    up = display.patch("jailbee.remote_display.display_up")

    result = CliRunner().invoke(app, ["display", "up", "--recreate"])

    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is True


def test_up_reports_a_display_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_up", side_effect=DisplayError("weston is dead"))

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 1
    assert "weston is dead" in result.output


def test_down_stops_the_display(display):
    down = display.patch("jailbee.remote_display.display_down")

    result = CliRunner().invoke(app, ["display", "down"])

    assert result.exit_code == 0, result.output
    down.assert_called_once()


def test_status_running_also_prints_the_recipe(display):
    display.patch("jailbee.remote_display.display_status", return_value=DisplayStatus.RUNNING)

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "ssh -N -L" in result.output


def test_status_stopped_prints_no_recipe(display):
    display.patch("jailbee.remote_display.display_status", return_value=DisplayStatus.STOPPED)

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "ssh -N -L" not in result.output


def test_down_reports_an_incus_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_down", side_effect=IncusError("stop failed"))

    result = CliRunner().invoke(app, ["display", "down"])

    assert result.exit_code == 1
    assert "stop failed" in result.output
    assert "Traceback" not in result.output


def test_status_reports_an_incus_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_status", side_effect=IncusError("incus gone"))

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 1
    assert "incus gone" in result.output
    assert "Traceback" not in result.output
