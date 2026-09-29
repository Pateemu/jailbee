"""Host-only LiteLLM CLI commands and their user-visible diagnostics."""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from jailbee import litellm as ll
from jailbee.cli import app
from jailbee.global_config import GlobalConfig

runner = CliRunner()


@pytest.fixture
def context(mocker):
    gcfg = GlobalConfig.model_validate({"litellm": {"enabled": True}})
    return mocker.patch("jailbee.cli._litellm_context", return_value=(MagicMock(), gcfg))


def _accounts(context, **litellm):
    gcfg = GlobalConfig.model_validate({"litellm": {"enabled": True, **litellm}})
    context.return_value = (context.return_value[0], gcfg)


def test_up_prints_endpoint_and_next_steps(mocker, context):
    up = mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=["default"],
            retired=[],
            installed=True,
            issues=["/x/repos/broken.yaml is broken"],
        ),
    )
    result = runner.invoke(app, ["litellm", "up", "--reinstall"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "10.0.0.3" in out and ":4100" in out
    assert "jailbee litellm login" in out
    assert "jailbee apply" in out
    assert "in-flight" in out
    assert "broken.yaml" in out
    assert up.call_args.kwargs["reinstall"] is True
    assert callable(up.call_args.kwargs["on_step"])


def test_up_disabled_is_exit_1_with_message(mocker, context):
    mocker.patch("jailbee.litellm.litellm_up", side_effect=ValueError("LiteLLM is disabled"))
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 1 and "disabled" in result.output
    assert "Traceback" not in result.output


def test_up_warns_if_install_is_unlocked(mocker, context):
    context.return_value[1].litellm.version = "1.104.0"
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 0
    assert "hash-locked" in result.output
    assert "in-flight" not in result.output


def test_up_names_restarted_and_retired_accounts(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.79.115.3",
            ports={"a": 4100, "b": 4101},
            restarted=["b"],
            retired=["old"],
            installed=False,
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Restarted b" in out
    assert "Stopped old" in out and "logins are kept" in out


def test_up_reports_a_missing_secret_as_a_clean_error(mocker, context):
    from jailbee.litellm_inputs import LiteLLMInputError

    mocker.patch(
        "jailbee.litellm.litellm_up",
        side_effect=LiteLLMInputError("OPENROUTER_API_KEY is not set in secrets.env"),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_down_removes_proxy_but_keeps_login(mocker, context):
    down = mocker.patch("jailbee.litellm.litellm_down")
    result = runner.invoke(app, ["litellm", "down"])
    assert result.exit_code == 0, result.output
    assert "logins and settings are kept" in " ".join(result.output.split())
    down.assert_called_once_with(context.return_value[0], purge=False)


def test_status_never_prints_tokens(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_status",
        return_value=ll.LiteLLMStatus(
            ll.ContainerState.RUNNING,
            "10.0.0.3",
            "1.103.0",
            [ll.InstanceStatus("default", 4100, True, True, "present")],
        ),
    )
    result = runner.invoke(app, ["litellm", "status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output and "logged in" in result.output
    assert "10.0.0.3" in result.output and "1.103.0" in result.output
    assert "4100" in result.output


@pytest.mark.parametrize(
    "status",
    [
        ll.LiteLLMStatus(ll.ContainerState.MISSING, None, None, []),
        ll.LiteLLMStatus(ll.ContainerState.STOPPED, "10.0.0.3", None, []),
        ll.LiteLLMStatus(ll.ContainerState.RUNNING, "10.0.0.3", "1.103.0", []),
        ll.LiteLLMStatus(
            ll.ContainerState.RUNNING,
            "10.0.0.3",
            "1.103.0",
            [ll.InstanceStatus("default", 4100, True, False, "missing")],
        ),
    ],
)
def test_status_exits_nonzero_when_proxy_is_unavailable(mocker, context, status):
    mocker.patch("jailbee.litellm.litellm_status", return_value=status)
    result = runner.invoke(app, ["litellm", "status"])
    assert result.exit_code == 1
    assert status.container.value in result.output
    if status.instances:
        assert "not logged in" in result.output
        assert "jailbee litellm login" in result.output


_TWO = {"accounts": ["personal", "work"], "profiles": {"codex": {"account": "personal"}}}


@pytest.mark.parametrize("command", ["login", "logout", "logs"])
def test_an_unknown_account_is_rejected_before_side_effects(mocker, context, command):
    target = {"login": "litellm_login", "logout": "litellm_logout", "logs": "litellm_logs"}[command]
    called = mocker.patch(f"jailbee.litellm.{target}")
    result = runner.invoke(app, ["litellm", command, "nope"])
    assert result.exit_code == 2
    assert "Unknown LiteLLM account 'nope'" in " ".join(result.output.split())
    called.assert_not_called()


@pytest.mark.parametrize("command", ["login", "logout", "logs"])
def test_several_accounts_need_a_name(mocker, context, command):
    _accounts(context, **_TWO)
    result = runner.invoke(app, ["litellm", command])
    assert result.exit_code == 2
    assert "personal, work" in " ".join(result.output.split())


def test_a_named_account_is_passed_through(mocker, context):
    _accounts(context, **_TWO)
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=0)
    runner.invoke(app, ["litellm", "logs", "work", "-f"])
    logs.assert_called_once_with(context.return_value[0], "work", follow=True)


def test_login_returns_device_flow_exit_code(mocker, context):
    login = mocker.patch("jailbee.litellm.litellm_login", return_value=19)
    result = runner.invoke(app, ["litellm", "login"])
    assert result.exit_code == 19
    login.assert_called_once_with(context.return_value[0], "default")


def test_logout(mocker, context):
    mocker.patch("jailbee.litellm.litellm_logout", return_value=True)
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 0 and "Logged out" in result.output


def test_logout_without_existing_auth(mocker, context):
    mocker.patch("jailbee.litellm.litellm_logout", return_value=False)
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 0 and "Not logged in" in result.output


def test_logs_passes_follow_and_exit_code(mocker, context):
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=17)
    result = runner.invoke(app, ["litellm", "logs", "-f"])
    assert result.exit_code == 17
    logs.assert_called_once_with(context.return_value[0], "default", follow=True)


def test_down_purge_is_passed_through(mocker, context):
    down = mocker.patch("jailbee.litellm.litellm_down")
    result = runner.invoke(app, ["litellm", "down", "--purge"])
    assert result.exit_code == 0, result.output
    down.assert_called_once_with(context.return_value[0], purge=True)
    assert "logins are gone" in " ".join(result.output.split())


def test_logout_needs_a_running_proxy(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_logout", side_effect=RuntimeError("run jailbee litellm up")
    )
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 1
    assert "jailbee litellm up" in result.output


def test_ls_lists_host_and_repo_blocks(mocker, tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    repos = tmp_path / "jailbee" / "repos"
    repos.mkdir(parents=True)
    (repos / "myrepo.yaml").write_text(
        yaml.safe_dump({"litellm": {"routes": {"sol-xhigh": {"effort": "max"}}}})
    )
    (repos / "broken.yaml").write_text(yaml.safe_dump({"litellm": {"default_profile": "nope"}}))
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate({"litellm": {"enabled": True}}),
    )
    result = runner.invoke(app, ["litellm", "ls"])
    assert result.exit_code == 0, result.output
    assert "codex*" in result.output
    assert "repo myrepo" in result.output and "jb-myrepo.<route>" in result.output
    # Rich folds long paths at the terminal width; compare without whitespace.
    assert "repos/broken.yaml" in "".join(result.output.split())


def test_ls_says_when_litellm_is_disabled(mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    result = runner.invoke(app, ["litellm", "ls"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output and "codex*" in result.output


def test_up_prints_an_issue_with_brackets_verbatim(mocker, context):
    issue = "/x/repos/a.yaml: routes.kimi [type=missing, input_type=dict] skipped"
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
            issues=[issue],
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert " ".join(issue.split()) in " ".join(result.output.split())
