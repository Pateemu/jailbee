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


@pytest.mark.parametrize(
    "override",
    [
        {"created": "x"},
        {"created": float("nan")},
        {"created": float("inf")},
        {"created": 4_000_000_000_000.0},
        {"created": True},
        {"port": "13389"},
        {"port": True},
        {"fingerprint": 5},
        {"host": None},
        {"container": 1},
    ],
)
def test_a_wrongly_typed_record_is_not_a_grant(override):
    import json

    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")
    record = json.loads(path.read_text()) | override
    path.write_text(json.dumps(record))

    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is False


def test_a_record_that_does_not_match_its_path_is_not_a_grant():
    import json

    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")
    record = json.loads(path.read_text()) | {"fingerprint": KEY_B}
    path.write_text(json.dumps(record))

    assert grants.is_allowed(KEY_A, "127.0.0.1", 13389) is False


def test_the_grants_directory_is_private_even_if_it_pre_existed():
    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")
    path.parent.chmod(0o755)

    grants.record_grant(KEY_B, "127.0.0.1", 13389, "feat-2")

    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_no_temporary_file_is_left_behind():
    path = grants.record_grant(KEY_A, "127.0.0.1", 13389, "feat-1")

    assert [item.name for item in path.parent.iterdir()] == [path.name]
