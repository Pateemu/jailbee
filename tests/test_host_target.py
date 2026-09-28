"""Tests for resolving the host's authoritative comparison target."""

from jailbee.host_target import resolve_target


def test_resolve_target_prefers_local_branch_when_head_is_detached(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse")
    rev_parse.side_effect = lambda _root, ref: {
        "refs/heads/vaaka-combined": "local-sha",
        "refs/remotes/origin/vaaka-combined": "tracking-sha",
    }.get(ref)
    is_ancestor = mocker.patch("jailbee.git.is_ancestor", return_value=False)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.sha == "local-sha"
    assert snapshot.source == "local"
    assert snapshot.tracking_relation == "diverged"
    assert snapshot.upstream_ref == "refs/remotes/origin/vaaka-combined"
    assert is_ancestor.call_count == 2


def test_resolve_target_uses_tracking_when_local_branch_is_absent(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse")
    rev_parse.side_effect = lambda _root, ref: {
        "refs/remotes/origin/vaaka-combined": "tracking-sha"
    }.get(ref)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.sha == "tracking-sha"
    assert snapshot.source == "tracking"
    assert snapshot.tracking_relation == "unavailable"


def test_resolve_target_is_unavailable_when_no_ref_resolves(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse", return_value=None)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.source == "unavailable"
    assert snapshot.sha is None
    assert snapshot.tracking_relation == "unavailable"
    assert rev_parse.call_count == 2


def test_resolve_target_marks_tracking_ahead_when_local_is_ancestor(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.rev_parse",
        side_effect=["local-sha", "tracking-sha"],
    )
    mocker.patch("jailbee.git.is_ancestor", side_effect=[True, False])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "tracking-ahead"


def test_resolve_target_marks_local_ahead_when_tracking_is_ancestor(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.rev_parse",
        side_effect=["local-sha", "tracking-sha"],
    )
    mocker.patch("jailbee.git.is_ancestor", side_effect=[False, True])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "local-ahead"


def test_resolve_target_marks_equal_refs(mocker, tmp_path):
    mocker.patch("jailbee.git.rev_parse", return_value="same-sha")

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "equal"


def test_resolve_target_marks_relation_unavailable_on_ancestor_error(mocker, tmp_path):
    mocker.patch("jailbee.git.rev_parse", side_effect=["local-sha", "tracking-sha"])
    mocker.patch("jailbee.git.is_ancestor", side_effect=[None, None])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.source == "local"
    assert snapshot.sha == "local-sha"
    assert snapshot.tracking_relation == "unavailable"
