from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from rich.console import Console

from jailbee import dashboard_accounts as da

ROWS = json.dumps(
    [
        {
            "agent": "claude",
            "group": "team",
            "account": "a@x.io#org12345",
            "state": "live",
            "repos": ["alpha"],
            "containers": ["alpha-x"],
        },
        {
            "agent": "claude",
            "group": None,
            "account": "b@x.io~2",
            "state": "parked",
            "repos": [],
            "containers": [],
        },
        {
            "agent": "claude",
            "group": "spare",
            "account": None,
            "state": "empty",
            "repos": [],
            "containers": [],
        },
    ]
)


def test_parse_account_rows_keeps_slot_names_verbatim() -> None:
    rows = da.parse_account_rows(ROWS)
    assert [r.account for r in rows] == ["a@x.io#org12345", "b@x.io~2", None]
    assert rows[0].repos == ("alpha",) and rows[1].group is None


@pytest.mark.parametrize("bad", ["", "{}", "null", "not json", '[{"agent": 1}]'])
def test_parse_account_rows_rejects_garbage_with_a_typed_error(bad: str) -> None:
    with pytest.raises(da.AccountLoadError):
        da.parse_account_rows(bad)


def test_empty_pool_parses_to_no_rows() -> None:
    assert da.parse_account_rows("[]") == ()


def test_missing_lists_default_to_empty() -> None:
    (row,) = da.parse_account_rows('[{"agent": "claude", "state": "parked"}]')
    assert row.repos == () and row.containers == () and row.group is None


def test_account_ls_argv_asks_for_the_parsed_fields() -> None:
    assert da.account_ls_argv() == [
        "account",
        "ls",
        "-o",
        "json",
        "--fields",
        "agent,group,account,state,repos,containers",
    ]


def test_argv_builders_keep_special_slot_names_as_single_elements() -> None:
    assert da.use_argv("claude", "team", "b@x.io#org12345~2") == [
        "account",
        "use",
        "b@x.io#org12345~2",
        "-a",
        "claude",
        "-g",
        "team",
    ]
    assert da.park_argv("claude", "team") == ["account", "park", "-a", "claude", "-g", "team"]
    assert da.rm_login_argv("claude", "b@x.io~2") == [
        "account",
        "rm",
        "b@x.io~2",
        "-a",
        "claude",
        "--yes",
    ]
    assert da.group_rm_argv("spare") == ["account", "group", "rm", "spare", "--yes"]
    assert da.group_create_argv("g") == ["account", "group", "create", "g"]
    assert da.repo_group_set_argv("none") == ["account", "group", "set", "none"]
    assert da.repo_group_unset_argv() == ["account", "group", "unset"]
    assert da.container_group_use_argv("team", "alpha-x") == [
        "account",
        "group",
        "use",
        "team",
        "alpha-x",
    ]
    assert da.container_group_reset_argv("alpha-x") == ["account", "group", "reset", "alpha-x"]


def test_group_names_and_parked_for() -> None:
    rows = da.parse_account_rows(ROWS)
    assert da.group_names(rows) == ("spare", "team")
    assert [r.account for r in da.parked_for(rows, "claude")] == ["b@x.io~2"]
    assert da.parked_for(rows, "codex") == ()


def test_actions_depend_on_the_row_kind() -> None:
    rows = da.parse_account_rows(ROWS)
    live, parked, empty = rows
    assert [a for _label, a in da.account_actions(live, rows)] == ["use", "park"]
    assert [a for _label, a in da.account_actions(parked, rows)] == ["use-in", "delete"]
    assert [a for _label, a in da.account_actions(empty, rows)] == ["use", "group-rm"]


def test_actions_omit_use_when_nothing_is_parked_and_group_rm_when_in_use() -> None:
    live = da.AccountRow("claude", "team", "a@x.io", "live", ("alpha",), ())
    assert [a for _label, a in da.account_actions(live, [live])] == ["park"]


def test_live_login_never_offers_group_rm_even_when_unused() -> None:
    live = da.AccountRow("claude", "team", "a@x.io", "live", (), ())
    assert [a for _label, a in da.account_actions(live, [live])] == ["park"]


def test_ungrouped_live_row_has_no_actions() -> None:
    own = da.AccountRow("claude", None, "a@x.io", "live", ("beta",), ())
    assert da.account_actions(own, [own]) == ()


def test_parked_row_offers_no_use_in_without_groups() -> None:
    parked = da.AccountRow("claude", None, "b@x.io", "parked", (), ())
    assert [a for _label, a in da.account_actions(parked, [parked])] == ["delete"]


def test_run_cli_quiet_reports_the_last_stderr_line_on_failure(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 2
    run.return_value.stdout = ""
    run.return_value.stderr = "warn\n\x1b[31merror: an agent is running; pass --force\x1b[0m\n"
    result = da.run_cli_quiet(["account", "group", "set", "g"], cwd=Path("/r"))
    assert result == da.CliResult(False, "error: an agent is running; pass --force")
    assert run.call_args.args[0] == ["jailbee", "account", "group", "set", "g"]
    assert run.call_args.kwargs["cwd"] == Path("/r")
    assert run.call_args.kwargs["capture_output"] is True


def test_run_cli_quiet_falls_back_to_exit_code(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 3
    run.return_value.stdout = ""
    run.return_value.stderr = ""
    assert da.run_cli_quiet(["x"], cwd=Path("/r")) == da.CliResult(False, "exited 3")


def test_run_cli_quiet_success_timeout_and_oserror(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 0
    run.return_value.stdout = "Switched.\n"
    run.return_value.stderr = ""
    assert da.run_cli_quiet(["account", "park"], cwd=Path("/r")) == da.CliResult(
        True, "Switched.", "Switched.\n"
    )
    run.side_effect = da.subprocess.TimeoutExpired(["jailbee"], 60)
    assert da.run_cli_quiet(["account", "park"], cwd=Path("/r")) == da.CliResult(False, "timed out")
    run.side_effect = FileNotFoundError("jailbee: not found")
    result = da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert not result.ok and "not found" in result.message


def test_accounts_state_navigation_and_render() -> None:
    state = da.AccountsState(da.parse_account_rows(ROWS))
    assert da.move_accounts(state, -1).index == 0
    assert da.move_accounts(state, 99).index == 2
    selected = da.selected_account(da.move_accounts(state, 1))
    assert selected is not None and selected.account == "b@x.io~2"
    console = Console(width=100, record=True)
    console.print(da.render_accounts(state))
    text = console.export_text()
    assert "team" in text and "a@x.io#org12345" in text and "parked" in text


def test_render_accounts_empty_and_markup_safe() -> None:
    console = Console(width=100, record=True)
    console.print(da.render_accounts(da.AccountsState(())))
    assert "no logins or groups on this host" in console.export_text()
    assert da.selected_account(da.AccountsState(())) is None
    weird = da.AccountRow("claude", "g[/x]", "[bold]a@x.io", "live", ("r[1]",), ())
    console = Console(width=100, record=True)
    console.print(da.render_accounts(da.AccountsState((weird,))))
    text = console.export_text()
    assert "[bold]a@x.io" in text and "g[/x]" in text


def test_run_cli_quiet_decodes_leniently(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 0
    run.return_value.stdout = "ok\n"
    run.return_value.stderr = ""
    da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert run.call_args.kwargs["errors"] == "replace"


def test_run_cli_quiet_turns_a_decode_error_into_a_failed_result(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    result = da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert result.ok is False and result.message
