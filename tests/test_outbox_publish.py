"""Selected publication exercises real candidate selection, domain gates and receipts."""

import json
from dataclasses import replace

import pytest

from jailbee import issue_github, issue_outbox, pr
from jailbee.outbox import io, service
from jailbee.outbox.inspect import build_views
from jailbee.outbox.io import PrManagement
from jailbee.outbox.models import ProposalId
from jailbee.outbox_io import JournalStore, journal_key, proposal_digest
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {"name": IDENTITY.full_name, "created_at": IDENTITY.created_at}
    ]
    incus.exec.return_value = ""
    labels = {"user.jailbee.pr": "42"}
    incus.config_get.side_effect = lambda c, key: labels.get(key)
    payload = json.loads(pr_files()["001.json"])
    payload["repo"] = "acme/repo"
    payload["actions"] = [
        {"type": "comment", "body_file": "body.md"},
        {"type": "comment", "body": "Second action"},
    ]
    snapshots = {
        "issue": store("issue", issue_files() | {"002.json": issue_files()["001.json"]}),
        "pr": store(
            "pr",
            {
                "001.json": json.dumps(payload),
                "002.json": json.dumps(payload),
                "body.md": "[red]Original[/red]\nLast line " + "x" * 100,
            },
        ),
    }

    def read(i, c, k, **kw):
        return snapshots[k]

    mocker.patch.object(service, "read_store", side_effect=read)
    mocker.patch.object(io, "read_store", side_effect=read)
    mocker.patch.object(
        issue_outbox, "read_text_outbox", side_effect=lambda *a, **kw: snapshots["issue"].as_dict()
    )
    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected real subprocess"))
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/repo.git")
    mocker.patch("jailbee.submodules.declared_submodule_remotes", return_value=())
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=[])
    mocker.patch.object(
        pr,
        "resolve_pr",
        return_value=pr.PrInfo(
            number=42, head_ref="feature", head_sha="a" * 40, state="OPEN", base_ref="main"
        ),
    )
    mocker.patch.object(pr, "gh_login", return_value="alice")
    mocker.patch.object(pr, "pr_body", return_value="Old description")
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    mocker.patch.object(issue_github, "list_labels", return_value={})
    fetch = mocker.patch.object(
        issue_github,
        "get_issue",
        return_value=issue_github.IssueSnapshot(
            42, "Old", "Old body", (), "open", "https://github.com/acme/repo/issues/42", False
        ),
    )
    mutations = {
        "create": mocker.patch.object(
            issue_github,
            "create_issue",
            return_value=issue_github.MutationReceipt(
                issue=73, url="https://github.com/acme/repo/issues/73"
            ),
        ),
        "comment": mocker.patch.object(
            issue_github,
            "add_comment",
            return_value=issue_github.MutationReceipt(
                issue=42, url="https://github.com/acme/repo/issues/42#issuecomment-1"
            ),
        ),
        "edit": mocker.patch.object(issue_github, "edit_issue"),
        "pr_comment": mocker.patch.object(
            pr,
            "add_issue_comment",
            return_value="https://github.com/acme/repo/pull/42#issuecomment-1",
        ),
        "pr_edit": mocker.patch.object(pr, "edit_pr"),
        "review": mocker.patch.object(
            pr,
            "submit_review",
            return_value="https://github.com/acme/repo/pull/42#pullrequestreview-1",
        ),
    }
    journals = JournalStore(tmp_path / "journals")
    manager = PrManagement(tmp_path / "pr-locks")
    # Same instance must own outer and delegated PR operations, never HOME.
    mocker.patch("jailbee.outbox.publish.PrManagement", return_value=manager)
    return cfg, incus, snapshots, journals, mutations, labels, fetch, manager


def selected(env, kind="issue", *, confirm=lambda count: True, revision=None, raise_errors=False, **options):
    from jailbee.outbox.publish import PublishOptions, publish_selected

    cfg, incus, _, journals, *_ = env
    return publish_selected(
        cfg,
        incus,
        IDENTITY.full_name,
        ProposalId(kind, "001.json"),
        journal_store=journals,
        options=PublishOptions(**options),
        confirm=confirm,
        expected_revision=revision,
        raise_errors=raise_errors,
    )


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("late", [False, True])
def test_typed_publication_preserves_validation_error(env, kind, late):
    from jailbee.outbox.models import OutboxError

    if kind == "issue":
        files = env[2][kind].as_dict()
        files["001.json"] = json.dumps({"version": 1, "actions": [{"type": "edit", "repo": ".", "issue": 42, "body": "New", "expected": {"body": "Old body"}}]})
        env[2][kind] = store(kind, files)
    def change():
        if kind == "issue":
            env[6].return_value = replace(env[6].return_value, body="Moved")
        else:
            env[5]["user.jailbee.pr"] = "43"
    if not late:
        change()
    def confirm(count):
        if late:
            change()
        return True
    with pytest.raises(OutboxError):
        selected(env, kind, confirm=confirm, raise_errors=True)
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_typed_publication_retains_snapshot_staleness(env, kind):
    from jailbee.outbox.models import OutboxChanged

    def confirm(count):
        env[2][kind] = store(kind, env[2][kind].as_dict() | {"body.md": "Changed"})
        return True
    with pytest.raises(OutboxChanged):
        selected(env, kind, confirm=confirm, raise_errors=True)
    assert_no_mutation(env)


@pytest.mark.parametrize("boundary", ["login", "issue", "labels", "outbox", "pr-login", "pr-read"])
def test_typed_publication_preserves_execution_cause(env, mocker, boundary):
    from jailbee.outbox.models import OutboxExecutionError
    from jailbee.outbox_io import OutboxReadError

    kind = "pr" if boundary.startswith("pr-") else "issue"
    failure = (pr.PrError("transport down") if kind == "pr" else OutboxReadError("transport down") if boundary == "outbox" else issue_github.IssueGithubReadError("transport down"))
    owner, name = {
        "login": (issue_github, "current_login"), "issue": (issue_github, "get_issue"),
        "labels": (issue_github, "list_labels"), "outbox": (issue_outbox, "read_issue_outbox"),
        "pr-login": (pr, "gh_login"), "pr-read": (pr, "resolve_pr"),
    }[boundary]
    mocker.patch.object(owner, name, side_effect=failure)
    with pytest.raises(OutboxExecutionError, match="transport down") as caught:
        selected(env, kind, raise_errors=True)
    cause = caught.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert cause is failure
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_typed_local_reader_failure_is_execution(env, mocker, kind):
    from jailbee.outbox.models import OutboxExecutionError
    from jailbee.incus import IncusTimeoutError

    failure = IncusTimeoutError("reader timed out")
    mocker.patch.object(service, "read_store", side_effect=failure)
    mocker.patch.object(io, "read_store", side_effect=failure)
    with pytest.raises(OutboxExecutionError):
        selected(env, kind, raise_errors=True)
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_typed_late_transport_is_execution_not_validation(env, mocker, kind):
    from jailbee.outbox.models import OutboxExecutionError

    if kind == "issue":
        files = env[2][kind].as_dict()
        files["001.json"] = json.dumps({"version": 1, "actions": [{"type": "edit", "repo": ".", "issue": 42, "body": "New", "expected": {"body": "Old body"}}]})
        env[2][kind] = store(kind, files)
    def confirm(count):
        if kind == "issue":
            env[6].side_effect = issue_github.IssueGithubReadError("late read failed")
        else:
            mocker.patch.object(pr, "resolve_pr", side_effect=pr.PrError("late read failed"))
        return True
    with pytest.raises(OutboxExecutionError, match="late read failed"):
        selected(env, kind, confirm=confirm, raise_errors=True)
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_typed_mutation_failure_retains_receipts_and_execution_type(env, kind):
    from jailbee.outbox.models import OutboxExecutionError

    if kind == "issue":
        env[4]["create"].side_effect = issue_github.IssueGithubMutationError("write failed", uncertain=True)
    else:
        env[4]["pr_comment"].side_effect = pr.PrError("write failed")
    detail = "outcome is uncertain" if kind == "issue" else "write failed"
    with pytest.raises(OutboxExecutionError, match=detail):
        selected(env, kind, raise_errors=True)
    if kind == "issue":
        assert env[3].load(journal_key(IDENTITY, "001.json")).actions[0].state == "uncertain"
    env[4]["review"].assert_not_called()


def revision(env, kind):
    return build_views(IDENTITY, (env[2][kind],), journal_store=env[3])[0].revision


def assert_no_mutation(env):
    for mutation in env[4].values():
        mutation.assert_not_called()
    env[1].exec.assert_not_called()
    assert not list(env[3].root.rglob("*.json"))


def test_issue_publishes_all_selected_actions_and_keeps_shared_body(env, capsys):
    counts = []

    def confirm(count):
        counts.append(count)
        text = capsys.readouterr().out
        assert "Host GitHub login: alice" in text
        assert "Repository: . (acme/repo)" in text
        assert "Original body" in text and "Follow-up" in text and "Independent" in text
        key = journal_key(IDENTITY, "001.json")
        assert env[3]._lock_path(key) in env[3]._held_lock_paths()
        return True

    assert selected(env, confirm=confirm) == 0
    assert counts == [3]
    env[4]["create"].assert_called_once_with(
        env[0].repo_root, "acme/repo", title="Example", body="Original body", labels=()
    )
    assert [call.args[2] for call in env[4]["comment"].call_args_list] == [73, 42]
    assert [call.kwargs["body"] for call in env[4]["comment"].call_args_list] == [
        "Follow-up",
        "Independent",
    ]
    removed = next(c.args[1] for c in env[1].exec.call_args_list if c.args[1][0] == "rm")
    assert any(n.endswith("/001.json") for n in removed)
    assert not any(n.endswith(("/002.json", "/body.md")) for n in removed)
    assert "fully applied" in capsys.readouterr().out
    assert env[3].load(journal_key(IDENTITY, "001.json")) is None
    assert env[3].load(journal_key(IDENTITY, "002.json")) is None


def test_pr_publishes_selected_all_actions_and_full_plain_text(env, capsys):
    def confirm(count):
        text = capsys.readouterr().out
        assert count == 2
        assert "alice" in text and "PR #42" in text
        assert "[red]Original[/red]" in text
        assert "Last line " + "x" * 100 in text
        assert "Second action" in text
        assert env[7].identity == IDENTITY
        return True

    assert selected(env, "pr", confirm=confirm) == 0
    assert [c.kwargs["repo"] for c in env[4]["pr_comment"].call_args_list] == [
        "acme/repo",
        "acme/repo",
    ]
    assert [c.args[2] for c in env[4]["pr_comment"].call_args_list] == [
        env[2]["pr"].as_dict()["body.md"],
        "Second action",
    ]
    removed = next(c.args[1] for c in env[1].exec.call_args_list if c.args[1][0] == "rm")
    assert not any(n.endswith(("/002.json", "/body.md")) for n in removed)
    assert env[7].identity is None


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("mode", ["cancel", "dry_run", "off_tty"])
def test_no_approval_or_dry_run_never_mutates(env, kind, mode):
    called = []
    assert (
        selected(env, kind, confirm=lambda n: called.append(n) or False, dry_run=mode == "dry_run")
        == 0
    )
    assert called == ([] if mode == "dry_run" else [3 if kind == "issue" else 2])
    assert_no_mutation(env)


def test_issue_force_is_refused(env):
    assert selected(env, force=True) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("when", ["before", "confirm"])
@pytest.mark.parametrize("change", ["body", "manifest", "identity", "progress", "rejected"])
def test_original_shared_revision_guards_fresh_inputs(env, kind, when, change):
    original = revision(env, kind)
    prepared = []

    def alter():
        snapshot = env[2][kind]
        files = snapshot.as_dict()
        if change == "identity":
            env[1].list_containers.return_value[0]["created_at"] = "replacement"
        elif change == "body":
            files["body.md"] = "Changed body"
        elif change == "manifest":
            files["001.json"] += " "
        elif change == "rejected":
            env[2][kind] = replace(snapshot, rejected=("body.md",))
            return
        elif kind == "pr":
            files["001.json.progress.json"] = '{"applied":[],"urls":{}}'
        else:
            env[3].create(journal_key(IDENTITY, "001.json"), "b" * 64, 3)
        env[2][kind] = store(kind, files)

    def confirm(count):
        prepared.append(count)
        alter()
        return True

    if when == "before":
        alter()
    assert selected(env, kind, revision=original, confirm=confirm) == 1
    assert prepared == ([] if when == "before" else [3 if kind == "issue" else 2])
    for mutation in env[4].values():
        mutation.assert_not_called()
    env[1].exec.assert_not_called()


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_domain_digest_is_not_a_ui_revision(env, kind):
    files = env[2][kind].as_dict()
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    assert selected(env, kind, revision=digest) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("rejected", ["001.json.progress.json", "applied.log"])
def test_pr_rejected_progress_is_not_lost_in_dict_conversion(env, rejected):
    env[2]["pr"] = replace(env[2]["pr"], rejected=(rejected,))
    assert selected(env, "pr") == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("submodule", [False, True])
def test_null_pr_requires_explicit_command_unless_fresh_gate_adopts(
    env, recorded, submodule, capsys, mocker
):
    payload = json.loads(env[2]["pr"].as_dict()["001.json"])
    payload["pr"] = None
    payload["actions"] = [{"type": "description", "body": "Draft"}]
    if submodule:
        from jailbee.pr_flow import PrRecord, PrScope
        from jailbee.submodule_pr import SubmodulePrState

        mocker.patch(
            "jailbee.pr_flow.candidate_scopes",
            return_value=[PrScope(env[0].repo_root / "lib", "origin", "lib", "lib")],
        )
        mocker.patch.object(
            SubmodulePrState,
            "read",
            return_value=PrRecord(
                number=42 if recorded else None, head=None, author=False, adopted=recorded
            ),
        )
    env[2]["pr"] = store("pr", {"001.json": json.dumps(payload)})
    if not recorded:
        env[5].clear()
    assert selected(env, "pr") == (0 if recorded else 1)
    if recorded:
        env[4]["pr_edit"].assert_called_once_with(
            env[0].repo_root / "lib" if submodule else env[0].repo_root,
            42,
            title=None,
            body="Draft",
            repo="acme/repo",
        )
    else:
        assert_no_mutation(env)
        assert ("jailbee submodule pr" if submodule else "jailbee pr") in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_domain_repo_gate_still_refuses(env, kind):
    files = env[2][kind].as_dict()
    payload = json.loads(files["001.json"])
    if kind == "pr":
        payload["repo"] = "evil/unowned"
    else:
        payload["actions"][0]["repo"] = "undeclared"
    files["001.json"] = json.dumps(payload)
    env[2][kind] = store(kind, files)
    assert selected(env, kind) == 1
    assert_no_mutation(env)


def test_pr_ownership_revalidated_after_confirm(env):
    def confirm(count):
        env[5]["user.jailbee.pr"] = "43"
        return True

    assert selected(env, "pr", confirm=confirm) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_before_prepare_stale_token_never_reaches_remote_gate(env, kind, mocker):
    token = revision(env, kind)
    env[2][kind] = replace(env[2][kind], rejected=("body.md",))
    login = mocker.patch.object(issue_github, "current_login")
    remote = mocker.patch.object(pr, "resolve_pr")
    assert selected(env, kind, revision=token) == 1
    login.assert_not_called()
    remote.assert_not_called()
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_identity_change_waiting_for_outer_lock_refuses(env, kind, mocker):
    from contextlib import contextmanager

    owner = env[3] if kind == "issue" else env[7]

    @contextmanager
    def replacing_lock(*args):
        env[1].list_containers.return_value[0]["created_at"] = "replacement"
        yield

    mocker.patch.object(owner, "lock", side_effect=replacing_lock)
    assert selected(env, kind) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("change", ["identity", "body"])
def test_change_inside_remote_revalidation_refuses(env, kind, change, mocker):
    confirmed = []

    def gate(*args, **kwargs):
        if confirmed:
            if change == "identity":
                env[1].list_containers.return_value[0]["created_at"] = "replacement"
            else:
                env[2][kind] = store(kind, env[2][kind].as_dict() | {"body.md": "Moved"})
        return pr.PrInfo(
            number=42, head_ref="feature", head_sha="a" * 40, state="OPEN", base_ref="main"
        )

    if kind == "pr":
        mocker.patch.object(pr, "resolve_pr", side_effect=gate)
    else:
        mocker.patch.object(issue_outbox, "revalidate_batch", side_effect=gate)
    assert selected(env, kind, confirm=lambda n: confirmed.append(n) or True) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_issue_uncertain_and_pr_partial_preserve_recovery(env, kind, capsys):
    if kind == "issue":
        env[4]["create"].side_effect = issue_github.IssueGithubMutationError(
            "timeout", uncertain=True
        )
        assert selected(env) == 1
        journal = env[3].load(journal_key(IDENTITY, "001.json"))
        assert journal.actions[0].state == "uncertain"
        output = capsys.readouterr()
        assert "001.json action 0: uncertain" in output.err
        assert "001.json action 0: uncertain" not in output.out
        assert "resolve: jailbee issue resolve" in output.out
        env[4]["comment"].assert_not_called()
    else:
        env[2]["pr"] = store(
            "pr",
            env[2]["pr"].as_dict()
            | {"001.json.progress.json": '{"applied":[0],"urls":{"0":"https://receipt"}}'},
        )
        assert selected(env, "pr") == 0
        assert [call.args[2] for call in env[4]["pr_comment"].call_args_list] == ["Second action"]


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_plan_cannot_interpret_terminal_controls(env, kind, capsys):
    files = env[2][kind].as_dict()
    files["body.md"] = "[red]Literal[/red]\nTail\x1b[31m"
    env[2][kind] = store(kind, files)
    assert selected(env, kind, dry_run=True) == 0
    text = capsys.readouterr().out
    assert "[red]Literal[/red]" in text and "Tail" in text
    assert "\x1b" not in text
    assert_no_mutation(env)


@pytest.mark.parametrize("mode", ["failed", "uncertain", "batch", "cleanup", "success"])
def test_actual_cli_and_selected_publication_share_outcome_streams(env, mocker, capsys, mode):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.outbox_io import JournalAction

    mocker.patch("jailbee.cli._load_or_exit", return_value=env[0])
    mocker.patch("jailbee.cli._resolve_existing", return_value=(env[1], IDENTITY.full_name))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feature")
    receipt = JournalAction(
        index=0,
        state="applied",
        repo="acme/repo",
        issue=73,
        url="https://github.com/acme/repo/issues/73",
    )
    skipped = replace(receipt, index=1, url="https://receipt/2")
    failure = (
        None
        if mode == "success"
        else issue_outbox.ApplyFailure(
            None if mode == "batch" else "001.json",
            1 if mode in ("failed", "uncertain") else None,
            mode == "uncertain",
            "Rejected mutation",
        )
    )
    report = issue_outbox.ApplyReport(
        applied=(("001.json", receipt),),
        skipped=(("001.json", skipped),) if mode == "success" else (),
        cleaned=("001.json",) if mode == "success" else (),
        failure=failure,
    )
    # This isolates rendering only; prepare and revalidation remain real.
    mocker.patch.object(issue_outbox, "apply_batch", return_value=report)
    assert selected(env) == (0 if mode == "success" else 1)
    selected_output = capsys.readouterr()
    result = CliRunner().invoke(
        app, ["issue", "apply", "feature", "--manifest", "001.json", "-y"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == (0 if mode == "success" else 1), result.output
    assert result.stderr == selected_output.err
    if mode == "success":
        assert result.stderr == ""
    else:
        expected = {
            "failed": "001.json action 1: failed",
            "uncertain": "001.json action 1: uncertain",
            "batch": "apply stopped:",
            "cleanup": "001.json: failed",
        }[mode]
        assert expected in result.stderr and expected not in result.stdout
    for text in (result.stdout, selected_output.out):
        assert "001.json action 0: applied (https://github.com/acme/repo/issues/73)" in text
        if mode == "success":
            assert "001.json action 1: applied (https://receipt/2)" in text
            assert "001.json: fully applied and removed from the outbox" in text
            assert (
                text.index("001.json action 0: applied")
                < text.index("001.json action 1: applied")
                < text.index("001.json: fully applied")
            )
        else:
            assert "001.json action 2: pending" in text
            assert text.index("001.json action 0: applied") < text.index(
                "001.json action 2: pending"
            )
        if mode == "uncertain":
            assert "resolve: jailbee issue resolve acme-feature 001.json 1" in text
            assert text.index("resolve: jailbee issue resolve") < text.index(
                "001.json action 2: pending"
            )


def test_issue_prepare_cannot_replace_original_preview(env, mocker):
    real = issue_outbox.prepare_batch

    def changed_prepare(*args, **kwargs):
        env[2]["issue"] = store("issue", env[2]["issue"].as_dict() | {"body.md": "Newer"})
        return real(*args, **kwargs)

    mocker.patch.object(issue_outbox, "prepare_batch", side_effect=changed_prepare)
    assert selected(env, revision=revision(env, "issue")) == 1
    assert_no_mutation(env)


@pytest.mark.parametrize("force", [False, True])
def test_pr_force_relaxes_only_moved_review_head(env, force, mocker):
    files = pr_files()
    payload = json.loads(files["001.json"])
    payload["repo"] = "acme/repo"
    files["001.json"] = json.dumps(payload)
    env[2]["pr"] = store("pr", files)
    mocker.patch.object(
        pr,
        "resolve_pr",
        return_value=pr.PrInfo(
            number=42, head_ref="feature", head_sha="b" * 40, state="OPEN", base_ref="main"
        ),
    )
    assert selected(env, "pr", force=force) == (0 if force else 1)
    if force:
        env[4]["review"].assert_called_once_with(
            env[0].repo_root,
            42,
            commit_id="a" * 40,
            body="Review body",
            comments=[
                {"path": "a.py", "line": 1, "body": "First", "side": "RIGHT"},
                {"path": "a.py", "line": 2, "body": "Second", "side": "RIGHT"},
            ],
            repo="acme/repo",
        )
    else:
        assert_no_mutation(env)


def test_pr_nested_lock_uses_same_instance_once(env, mocker):
    flock = mocker.spy(io.fcntl, "flock")
    assert selected(env, "pr") == 0
    assert [call.args[1] for call in flock.call_args_list] == [io.fcntl.LOCK_EX, io.fcntl.LOCK_UN]


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_missing_selected_manifest_is_failure_not_empty_success(env, kind):
    files = env[2][kind].as_dict()
    files.pop("001.json")
    env[2][kind] = store(kind, files)
    assert selected(env, kind) == 1
    assert_no_mutation(env)


def test_issue_expected_fields_revalidated_after_confirm(env):
    files = env[2]["issue"].as_dict()
    payload = {
        "version": 1,
        "actions": [
            {
                "type": "edit",
                "repo": ".",
                "issue": 42,
                "body": "New",
                "expected": {"body": "Old body"},
            }
        ],
    }
    files["001.json"] = json.dumps(payload)
    env[2]["issue"] = store("issue", files)

    def confirm(count):
        env[6].return_value = replace(env[6].return_value, body="Remote moved")
        return True

    assert selected(env, confirm=confirm) == 1
    assert_no_mutation(env)
