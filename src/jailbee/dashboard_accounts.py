"""Credential groups and logins for the dashboard: rows, argv builders, runner.

Everything here is pure except `run_cli_quiet`, the one subprocess seam. The
dashboard drives account changes by re-executing `jailbee`, never by reaching
into `accounts/` — the CLI stays the single place that knows the pool rules
(locks, running-agent refusals, parking). This module only shapes the rows the
listing prints and the argv the actions run.

Must not import `jailbee.dashboard` or `jailbee.dashboard_overlays`: the
dependency direction is `dashboard_overlays -> dashboard_accounts`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jailbee.dashboard_settings import CURSOR_STYLE

if TYPE_CHECKING:
    from rich.console import RenderableType

ACCOUNT_LS_FIELDS = "agent,group,account,state,repos,containers"

ACCOUNTS_HINT = (
    "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] actions  ·  "
    "[bold]n[/bold] new group  ·  [bold]Esc[/bold] close"
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class AccountLoadError(Exception):
    """`jailbee account ls` printed something that is not a list of rows."""


@dataclass(frozen=True)
class AccountRow:
    """One line of `account ls`. `account` is the slot name `use`/`rm` take."""

    agent: str
    group: str | None
    account: str | None
    state: str  # "live" | "parked" | "empty"
    repos: tuple[str, ...]
    containers: tuple[str, ...]


@dataclass(frozen=True)
class CliResult:
    """Outcome of a quiet CLI run: `message` for a notice, `stdout` for loaders."""

    ok: bool
    message: str
    stdout: str = ""


@dataclass(frozen=True)
class AccountsState:
    rows: tuple[AccountRow, ...]
    index: int = 0
    prefix: str = ""


def account_ls_argv() -> list[str]:
    return ["account", "ls", "-o", "json", "--fields", ACCOUNT_LS_FIELDS]


def _bad_output() -> AccountLoadError:
    return AccountLoadError("unexpected output from 'jailbee account ls'")


def _str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad_output()
    return tuple(value)


def _opt_str(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _bad_output()
    return value


def parse_account_rows(stdout: str) -> tuple[AccountRow, ...]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _bad_output() from exc
    if not isinstance(data, list):
        raise _bad_output()
    rows: list[AccountRow] = []
    for item in data:
        if not isinstance(item, dict):
            raise _bad_output()
        agent, state = item.get("agent"), item.get("state")
        if not isinstance(agent, str) or not isinstance(state, str):
            raise _bad_output()
        rows.append(
            AccountRow(
                agent=agent,
                group=_opt_str(item.get("group")),
                account=_opt_str(item.get("account")),
                state=state,
                repos=_str_tuple(item.get("repos")),
                containers=_str_tuple(item.get("containers")),
            )
        )
    return tuple(rows)


def _last_line(text: str) -> str:
    lines = [_ANSI.sub("", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[-1] if lines else ""


def run_cli_quiet(argv: Sequence[str], *, cwd: Path, timeout: float = 60.0) -> CliResult:
    """Run `jailbee <argv>` capturing all output; never touches the terminal."""
    try:
        proc = subprocess.run(
            ["jailbee", *argv],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CliResult(False, "timed out")
    except OSError as exc:
        return CliResult(False, str(exc))
    if proc.returncode == 0:
        return CliResult(True, _last_line(proc.stdout) or "done", proc.stdout)
    message = _last_line(proc.stderr) or _last_line(proc.stdout) or f"exited {proc.returncode}"
    return CliResult(False, message)


def use_argv(agent: str, group: str | None, ref: str) -> list[str]:
    argv = ["account", "use", ref, "-a", agent]
    if group is not None:
        argv += ["-g", group]
    return argv


def park_argv(agent: str, group: str | None) -> list[str]:
    argv = ["account", "park", "-a", agent]
    if group is not None:
        argv += ["-g", group]
    return argv


def rm_login_argv(agent: str, ref: str) -> list[str]:
    return ["account", "rm", ref, "-a", agent, "--yes"]


def group_create_argv(name: str) -> list[str]:
    return ["account", "group", "create", name]


def group_rm_argv(name: str) -> list[str]:
    return ["account", "group", "rm", name, "--yes"]


def repo_group_set_argv(group: str) -> list[str]:
    return ["account", "group", "set", group]


def repo_group_unset_argv() -> list[str]:
    return ["account", "group", "unset"]


def container_group_use_argv(group: str, container: str) -> list[str]:
    return ["account", "group", "use", group, container]


def container_group_reset_argv(container: str) -> list[str]:
    return ["account", "group", "reset", container]


def group_names(rows: Sequence[AccountRow]) -> tuple[str, ...]:
    return tuple(sorted({r.group for r in rows if r.group is not None}))


def parked_for(rows: Sequence[AccountRow], agent: str) -> tuple[AccountRow, ...]:
    return tuple(r for r in rows if r.state == "parked" and r.agent == agent)


def account_actions(row: AccountRow, rows: Sequence[AccountRow]) -> tuple[tuple[str, str], ...]:
    """`(label, action_id)` pairs the picker offers for `row`."""
    if row.state == "parked":
        actions: list[tuple[str, str]] = []
        if group_names(rows):
            actions.append(("Use in a group…", "use-in"))
        actions.append(("Delete this login…", "delete"))
        return tuple(actions)
    if row.group is None:
        return ()
    actions = []
    if parked_for(rows, row.agent):
        actions.append(("Use a stored login…", "use"))
    if row.state == "live":
        actions.append(("Park the live login", "park"))
    elif row.state == "empty" or (not row.repos and not row.containers):
        actions.append(("Remove this group", "group-rm"))
    return tuple(actions)


def move_accounts(state: AccountsState, delta: int) -> AccountsState:
    if not state.rows:
        return state
    index = max(0, min(len(state.rows) - 1, state.index + delta))
    return AccountsState(state.rows, index, state.prefix)


def selected_account(state: AccountsState) -> AccountRow | None:
    if 0 <= state.index < len(state.rows):
        return state.rows[state.index]
    return None


def render_accounts(state: AccountsState) -> RenderableType:
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    for header in ("GROUP", "AGENT", "ACCOUNT", "STATE", "USED BY"):
        table.add_column(header, overflow="ellipsis", no_wrap=True)
    for i, row in enumerate(state.rows):
        style = CURSOR_STYLE if i == state.index else ""
        used_by = ", ".join((*row.repos, *row.containers)) or "-"
        cells = (row.group or "-", row.agent, row.account or "-", row.state, used_by)
        table.add_row(*(Text(c) for c in cells), style=style)
    body: RenderableType = table
    if not state.rows:
        body = Text.from_markup("[dim](no logins or groups on this host)[/dim]")
    return Panel(body, title="credential groups and logins", box=box.ROUNDED)
