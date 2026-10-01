"""Forwarding grants are bound to the SSH key, the destination and an age limit."""

from __future__ import annotations

import pytest

from jailbee.remote_ssh import display_grants as grants

KEY_A = "SHA256:keyA"
KEY_B = "SHA256:keyB"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_a_recorded_grant_allows_exactly_that_key_and_destination():
    grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")

    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is True


def test_another_key_is_refused():
    """Review focus 1."""
    grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")

    assert grants.is_allowed(KEY_B, "127.0.0.1", 13389) is False


@pytest.mark.parametrize(
    ("host", "port"), [("127.0.0.1", 22), ("10.0.0.5", 13389), ("localhost", 13389)]
)
def test_another_destination_is_refused(host, port):
    grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")

    assert grants.is_allowed(KEY_A, host, port) is False


def test_nothing_is_allowed_without_a_grant():
    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is False


def test_a_grant_expires_and_is_pruned():
    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1", now=1000.0)

    late = 1000.0 + grants.GRANT_MAX_AGE_SECONDS + 1
    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389, now=late) is False
    assert not path.exists()


def test_clear_grants_revokes_everything():
    grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")
    grants.record_grant(KEY_B, "127.0.0.1", 13389, "feat-2")

    grants.clear_grants()

    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is False
    assert grants.is_allowed(KEY_B, "127.0.0.1", 13389) is False


def test_a_corrupt_record_is_not_a_grant():
    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")
    path.write_text("not json")

    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is False


def test_the_record_is_private():
    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")

    assert path.stat().st_mode & 0o777 == 0o600
