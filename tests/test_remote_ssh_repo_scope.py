"""Trusted host snapshot for repository visibility in SSH descendants."""

import json

import pytest

from jailbee.remote_ssh.repo_scope import RepoScopeError, scope_for_session
from jailbee.remote_ssh.session import REMOTE_SESSION_ENV, SSH_SESSION_ENV, child_environment


def test_local_process_allows_every_repo() -> None:
    assert scope_for_session({}).allows("anything")


@pytest.mark.parametrize("snapshot", [None, "not-json", "{}", '["bad/name"]'])
def test_ssh_process_rejects_missing_or_invalid_snapshot(snapshot: str | None) -> None:
    environ = {SSH_SESSION_ENV: "1"}
    if snapshot is not None:
        environ["JAILBEE_SSH_EXCLUDED_REPOS"] = snapshot
    with pytest.raises(RepoScopeError):
        scope_for_session(environ)


def test_ssh_process_uses_valid_snapshot() -> None:
    scope = scope_for_session({SSH_SESSION_ENV: "1", "JAILBEE_SSH_EXCLUDED_REPOS": '["secret"]'})
    assert not scope.allows("secret")
    assert scope.allows("public")


def test_child_environment_replaces_inherited_snapshot_even_when_empty() -> None:
    env = child_environment({"JAILBEE_SSH_EXCLUDED_REPOS": '["attacker"]'}, excluded_repos=())
    assert env["JAILBEE_SSH_EXCLUDED_REPOS"] == "[]"
    assert json.loads(env["JAILBEE_SSH_EXCLUDED_REPOS"]) == []


def test_remote_marker_also_requires_snapshot() -> None:
    with pytest.raises(RepoScopeError):
        scope_for_session({REMOTE_SESSION_ENV: "1"})
