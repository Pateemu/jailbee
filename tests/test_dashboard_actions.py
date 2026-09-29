"""Tests for the dashboard's entries for existing CLI verbs (pure; no terminal)."""

from __future__ import annotations

import pytest

from jailbee import dashboard_actions as dact
from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import prompt_target_kind

PolicyCase = tuple[bool, dict[str, object] | None]


def _policy(kwargs: dict[str, object] | None) -> RemoteSSHConfig | None:
    return None if kwargs is None else RemoteSSHConfig.model_validate(kwargs)


# The cases every entry is checked against: (over_ssh, RemoteSSHConfig kwargs).
_LOCAL: PolicyCase = (False, None)
# A local dashboard never consults the SSH policy, however strict.
_LOCAL_DISABLED: PolicyCase = (False, {"commands": {"mode": "disabled"}})
# `full` commands under restrict_host: true, what `remote.ssh` defaults to.
_SSH_DEFAULT: PolicyCase = (True, {})
_SSH_ALLOW_OTHER: PolicyCase = (True, {"commands": {"mode": "allowlist", "allow": ["shell"]}})
_SSH_EXCLUDED: PolicyCase = (True, {"excluded_repos": ["other"]})
# An SSH dashboard started without a server policy fails closed.
_SSH_NO_POLICY: PolicyCase = (True, None)


def _allow(*leaves: str, restrict_host: bool = True) -> PolicyCase:
    return (
        True,
        {"commands": {"mode": "allowlist", "allow": list(leaves)}, "restrict_host": restrict_host},
    )


def _repo_verbs(case: PolicyCase) -> set[str]:
    over_ssh, kwargs = case
    extras = dact.repo_extras(_policy(kwargs), over_ssh=over_ssh)
    leaves = [extras.apply, *extras.diagnostics, extras.prune]
    return {leaf[1] for leaf in leaves if leaf is not None}


_ALL_REPO = {"apply", "doctor", "disk-usage", "prune"}


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (_LOCAL, _ALL_REPO),
        (_LOCAL_DISABLED, _ALL_REPO),
        # `apply` manages the host: refused under restrict_host in every mode
        (_SSH_DEFAULT, _ALL_REPO - {"apply"}),
        (_allow("apply", "doctor", "disk-usage", "prune"), _ALL_REPO - {"apply"}),
        (_allow("apply", restrict_host=False), {"apply"}),
        (_allow("doctor"), {"doctor"}),
        (_allow("disk-usage"), {"disk-usage"}),
        (_allow("prune"), {"prune"}),
        (_SSH_ALLOW_OTHER, set()),
        # repository exclusions leave only the router's single-repo `safe` set
        (_SSH_EXCLUDED, {"prune"}),
        (_SSH_NO_POLICY, set()),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-restricted",
        "ssh-allowlist-apply-unrestricted",
        "ssh-allowlist-doctor",
        "ssh-allowlist-disk-usage",
        "ssh-allowlist-prune",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
        "ssh-no-policy",
    ],
)
def test_repo_extras_follow_the_ssh_policy(case, expected):
    assert _repo_verbs(case) == expected


def test_repo_extras_labels_and_submenu_order():
    extras = dact.repo_extras(None, over_ssh=False)
    assert extras.apply == ("Apply config…", "apply")
    assert extras.diagnostics == (("Doctor", "doctor"), ("Disk usage", "disk-usage"))
    assert extras.prune == ("Prune stale containers…", "prune")


def test_repo_argv_never_answers_the_clis_own_questions():
    assert dact.apply_argv(no_restart=False) == ["apply"]
    assert dact.apply_argv(no_restart=True) == ["apply", "--no-restart"]
    assert dact.prune_argv() == ["prune"]  # no --yes-to-all: prune asks per container
    assert dact.doctor_argv() == ["doctor"]
    assert dact.disk_usage_argv() == ["disk-usage"]


def test_apply_picker_is_a_repo_question_with_restart_first():
    picker = dact.apply_picker("alpha")
    assert picker.purpose == "repo-apply"
    assert prompt_target_kind(picker.purpose) == "repo"
    assert picker.target == "alpha"
    assert [e.value for e in picker.entries] == [dact.APPLY_RESTART, dact.APPLY_NO_RESTART]


@pytest.mark.parametrize(
    ("over_ssh", "expected"),
    [
        (False, ["snapshot", "create", "--config", "/r/c.yaml", "--", "alpha-x", "t"]),
        (True, ["snapshot", "create", "--", "alpha-x", "t"]),
    ],
    ids=["local", "ssh"],
)
def test_addressed_puts_config_before_the_separator_and_never_over_ssh(over_ssh, expected):
    argv = ["snapshot", "create", "--", "alpha-x", "t"]
    assert dact.addressed(argv, ["--config", "/r/c.yaml"], over_ssh=over_ssh) == expected
    assert argv == ["snapshot", "create", "--", "alpha-x", "t"]  # not mutated


def test_addressed_appends_config_when_there_is_no_separator():
    assert dact.addressed(["apply"], ["--config", "/c"], over_ssh=False) == [
        "apply",
        "--config",
        "/c",
    ]


def test_command_label_stops_at_the_first_option():
    assert dact.command_label(["snapshot", "create", "--", "a", "b"]) == "snapshot create"
    assert dact.command_label(["apply", "--no-restart"]) == "apply"
    assert dact.command_label(["autostart", "status", "alpha-x"]) == "autostart status alpha-x"
