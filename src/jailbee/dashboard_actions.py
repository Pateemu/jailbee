"""Terminal-dashboard entries for existing `jailbee` verbs: argv, gating, questions.

The repo menu gains `apply`, `doctor`, `disk-usage` and `prune`; the container
menu gains the autostart run, snapshots and optional mounts. Everything here
is pure. `jailbee.dashboard` wires it into `run()`, and every command runs as
a real `jailbee` child, so the CLI stays the one place that validates a tag, a
mount kind or a restart. This module only decides which entries a row offers
and which argv each one runs.

Visibility follows the remote-SSH policy exactly. An entry is offered only
when `dashboard_commands.permitted` accepts the argv shape it will run, and
the spawn re-checks the real argv anyway. Locally (`over_ssh` false) the
policy is never consulted.

Must not import `jailbee.dashboard`, which imports this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import takewhile
from typing import TYPE_CHECKING

from jailbee.dashboard_commands import insert_options_before_separator, permitted
from jailbee.dashboard_overlays import Picker, PickerEntry

if TYPE_CHECKING:
    from jailbee.config.models_remote import RemoteSSHConfig

Leaf = tuple[str, str]
"""A menu entry, ``(label, verb)``. The verbs below are handled by `run()` itself."""

REPO_APPLY = "apply"
REPO_DOCTOR = "doctor"
REPO_DISK_USAGE = "disk-usage"
REPO_PRUNE = "prune"

DIAGNOSTICS_LABEL = "Diagnostics →"

APPLY_RESTART = "restart"
APPLY_NO_RESTART = "no-restart"


def apply_argv(*, no_restart: bool) -> list[str]:
    """`jailbee apply`, never with `--yes`: its restart question is the user's.

    That question is also why it runs in the foreground: `apply` asks before
    restarting a container or its dockerd, and only a terminal can answer.
    """
    return ["apply", *(["--no-restart"] if no_restart else [])]


def doctor_argv() -> list[str]:
    return ["doctor"]


def disk_usage_argv() -> list[str]:
    return ["disk-usage"]


def prune_argv() -> list[str]:
    """`jailbee prune` without `--yes-to-all`: its per-container questions confirm it."""
    return ["prune"]


@dataclass(frozen=True)
class RepoExtras:
    """The repo-menu entries this module adds, already filtered by the SSH policy.

    ``diagnostics`` becomes the ``Diagnostics →`` submenu, omitted when empty.
    """

    apply: Leaf | None
    diagnostics: tuple[Leaf, ...]
    prune: Leaf | None


def repo_extras(ssh_policy: RemoteSSHConfig | None, *, over_ssh: bool) -> RepoExtras:
    """What an actionable repo's menu adds; each entry hidden when its command would be refused."""

    def offer(leaf: Leaf, argv: list[str]) -> Leaf | None:
        return leaf if permitted(argv, ssh_policy, over_ssh=over_ssh) else None

    diagnostics = (
        offer(("Doctor", REPO_DOCTOR), doctor_argv()),
        offer(("Disk usage", REPO_DISK_USAGE), disk_usage_argv()),
    )
    return RepoExtras(
        apply=offer(("Apply config…", REPO_APPLY), apply_argv(no_restart=False)),
        diagnostics=tuple(leaf for leaf in diagnostics if leaf is not None),
        prune=offer(("Prune stale containers…", REPO_PRUNE), prune_argv()),
    )


def apply_picker(prefix: str) -> Picker:
    """Whether `apply` may restart what it changes. Esc runs nothing."""
    return Picker(
        "repo-apply",
        f"Apply config — {prefix}",
        (
            PickerEntry("Apply (asks before restarting anything)", APPLY_RESTART),
            PickerEntry("Apply without restarting (--no-restart)", APPLY_NO_RESTART),
        ),
        target=prefix,
    )


def addressed(argv: Sequence[str], flags: Sequence[str], *, over_ssh: bool) -> list[str]:
    """``argv`` pointed at its repo: ``flags`` (``--config``) go before any ``--``.

    Over SSH nothing is added. The child is addressed by its cwd alone, because
    a host config path is exactly the host-reaching argument the remote policy
    refuses (`router.check_arguments`).
    """
    if over_ssh:
        return list(argv)
    return insert_options_before_separator(list(argv), flags)


def command_label(argv: Sequence[str]) -> str:
    """The words of ``argv`` up to its first option or ``--``, for an exit notice."""
    return " ".join(takewhile(lambda word: not word.startswith("-"), argv))
