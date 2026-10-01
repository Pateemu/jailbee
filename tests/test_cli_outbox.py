"""Unified outbox contracts exercised through the real root Typer app."""

import json

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.incus import Incus
from jailbee.outbox import service
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, container_prefix="acme")
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.config.load_config", return_value=cfg)
    incus = mocker.Mock(spec=Incus)
    raw = {
        "name": IDENTITY.full_name,
        "created_at": IDENTITY.created_at,
        "profiles": ["acme-base"],
        "status": "Running",
    }
    incus.list_containers.return_value = [raw]
    incus.exists.side_effect = lambda name: name == IDENTITY.full_name
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    snapshots = {"pr": store("pr", pr_files()), "issue": store("issue", issue_files())}
    reader = mocker.patch.object(
        service, "read_store", side_effect=lambda i, c, k, **kw: snapshots[k]
    )
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    journals = JournalStore(tmp_path / "journals")
    mocker.patch("jailbee.outbox_io.JournalStore", return_value=journals)
    return cfg, incus, snapshots, reader, mutation, journals, raw


@pytest.mark.parametrize("leaf", [None, "browse", "ls", "show", "drop", "apply"])
def test_public_help(leaf):
    args = ["outbox"] + ([leaf] if leaf else []) + ["--help"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    if leaf is None:
        assert all(name in result.output for name in ("browse", "ls", "show", "drop", "apply"))
    if leaf == "drop":
        assert "zero-based" in result.output
        assert "--revision" in result.output


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["outbox"], ["outbox", "browse"]),
        (["outbox", "feature"], ["outbox", "browse", "feature"]),
        (["outbox", "ls", "feature"], ["outbox", "ls", "feature"]),
        (["outbox", "--help"], ["outbox", "--help"]),
        (
            ["outbox", "--config", "ls", "feature"],
            ["outbox", "--config", "ls", "browse", "feature"],
        ),
        (["outbox", "--config=x", "ls"], ["outbox", "--config=x", "ls"]),
        (["--version"], ["--version"]),
    ],
)
def test_normalize(argv, expected):
    from jailbee.cli_outbox import normalize_outbox_argv

    assert normalize_outbox_argv(argv) == expected


@pytest.mark.parametrize("args", [[], ["feature"], ["browse", "feature"]])
def test_browser_overview_without_prompt(env, mocker, args):
    prompt = mocker.patch("typer.confirm", side_effect=AssertionError("inspection prompted"))
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 0, result.output
    assert "issue/001.json" in result.output and "pr/001.json" in result.output
    prompt.assert_not_called()


@pytest.mark.parametrize("option", ["--format", "--output", "-o"])
@pytest.mark.parametrize("leaf", ["ls", "show"])
def test_json_aliases(env, option, leaf):
    args = ["outbox", leaf, "feature"] + (["issue/001.json"] if leaf == "show" else [])
    result = CliRunner().invoke(app, [*args, option, "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["schema"] == 1
    if leaf == "show":
        assert data["proposal"]["actions"][0]["index"] == 0
    else:
        assert data["containers"][0]["name"] == IDENTITY.full_name


@pytest.mark.parametrize(
    "args",
    [
        ["--config", "fixture.yaml", "ls"],
        ["ls", "--config", "fixture.yaml"],
        ["--config=fixture.yaml", "feature"],
    ],
)
def test_group_and_leaf_config_placement(env, mocker, args):
    loader = mocker.patch("jailbee.config.load_config", return_value=env[0])
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 0, result.output
    assert loader.call_args.args[0].name == "fixture.yaml"


@pytest.mark.parametrize(
    "args",
    [
        ["ls", "-o", "xml"],
        ["show", "feature", "issue/001.json", "-o", "xml"],
        ["drop", "feature", "../001.json", "-y"],
        ["apply", "feature", "issue/001.json", "--force", "-y"],
        ["drop", "feature", "issue/001.json", "--comment", "0", "-y"],
        ["drop", "feature", "issue/001.json", "--action", "0", "-y"],
        ["ls", "feature", "--all-repos"],
    ],
)
def test_validation_never_mutates(env, args):
    result = CliRunner().invoke(app, ["outbox", *args])
    assert result.exit_code == 2, result.output
    env[4].assert_not_called()
    env[1].start.assert_not_called()


@pytest.mark.parametrize("leaf", ["drop", "apply"])
def test_stale_revision_is_validation_error(env, leaf):
    result = CliRunner().invoke(
        app, ["outbox", leaf, "feature", "issue/001.json", "--revision", "stale", "-y"]
    )
    assert result.exit_code == 2, result.output
    assert "refresh" in result.output
    env[4].assert_not_called()


def test_drop_cancel_and_exact_confirmed_cascade(env):
    args = ["outbox", "drop", "feature", "issue/001.json", "--action", "0", "--with-dependents"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert "(0, 1)" in result.output
    env[4].assert_not_called()
    result = CliRunner().invoke(app, [*args, "-y"])
    assert result.exit_code == 0, result.output
    payload = json.loads(env[4].call_args.kwargs["new_manifest"][1])
    assert payload["actions"] == [
        {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"}
    ]


def test_drop_execution_error_maps_to_one(env):
    from jailbee.outbox.models import OutboxExecutionError

    env[4].side_effect = OutboxExecutionError("write failed")
    result = CliRunner().invoke(app, ["outbox", "drop", "feature", "issue/001.json", "-y"])
    assert result.exit_code == 1, result.output
    assert "write failed" in result.output


@pytest.mark.parametrize("name", [[], ["feature"]])
def test_stopped_is_structured_unavailable(env, name):
    env[6]["status"] = "Stopped"
    result = CliRunner().invoke(app, ["outbox", "ls", *name, "-o", "json"])
    assert result.exit_code == 2, result.output
    row = json.loads(result.stdout)["containers"][0]
    assert row["available"] is False and "stopped" in row["error"].lower()
    env[3].assert_not_called()


def test_literal_untrusted_detail(env):
    files = issue_files()
    files["body.md"] = "[bold]literal[/bold]\x1b\r‮ tail"
    env[2]["issue"] = store("issue", files)
    result = CliRunner().invoke(app, ["outbox", "show", "feature", "issue/001.json"])
    assert result.exit_code == 0, result.output
    assert "[bold]literal[/bold]" in result.output
    assert "\x1b" not in result.output and "‮" not in result.output


def test_typo_is_missing_container_not_silent_overview(env):
    result = CliRunner().invoke(app, ["outbox", "lss"])
    assert result.exit_code == 2
    assert "no such container" in result.output
    env[3].assert_not_called()


@pytest.fixture
def publication_env(env, mocker, tmp_path):
    from jailbee import issue_github, issue_outbox, pr
    from jailbee.outbox import io
    from jailbee.outbox.io import PrManagement

    mocker.patch.object(io, "read_store", side_effect=lambda i, c, k, **kw: env[2][k])
    mocker.patch.object(
        issue_outbox, "read_text_outbox", side_effect=lambda *a, **kw: env[2]["issue"].as_dict()
    )
    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected subprocess"))
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/repo.git")
    mocker.patch("jailbee.submodules.declared_submodule_remotes", return_value=())
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=[])
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    mocker.patch.object(issue_github, "list_labels", return_value={})
    mocker.patch.object(
        issue_github,
        "get_issue",
        return_value=issue_github.IssueSnapshot(42, "Old", "Body", (), "open", "url", False),
    )
    create = mocker.patch.object(
        issue_github,
        "create_issue",
        return_value=issue_github.MutationReceipt(
            issue=73, url="https://github.com/acme/repo/issues/73"
        ),
    )
    comment = mocker.patch.object(
        issue_github,
        "add_comment",
        return_value=issue_github.MutationReceipt(
            issue=42, url="https://github.com/acme/repo/issues/42#issuecomment-1"
        ),
    )
    review = mocker.patch.object(
        pr, "submit_review", return_value="https://github.com/acme/repo/pull/42#pullrequestreview-1"
    )
    mocker.patch.object(pr, "gh_login", return_value="alice")
    mocker.patch.object(
        pr,
        "resolve_pr",
        return_value=pr.PrInfo(
            number=42, head_ref="feature", head_sha="a" * 40, state="OPEN", base_ref="main"
        ),
    )
    env[1].config_get.side_effect = lambda c, key: "42" if key == "user.jailbee.pr" else None
    env[1].exec.return_value = ""
    payload = json.loads(env[2]["pr"].as_dict()["001.json"])
    payload["repo"] = "acme/repo"
    env[2]["pr"] = store("pr", {"001.json": json.dumps(payload), "002.json": json.dumps(payload)})
    files = env[2]["issue"].as_dict()
    env[2]["issue"] = store("issue", files | {"002.json": files["001.json"]})
    manager = PrManagement(tmp_path / "pr-locks")
    mocker.patch("jailbee.outbox.publish.PrManagement", return_value=manager)
    return env, create, comment, review


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("mode", ["yes", "cancel", "dry-run"])
def test_apply_real_domain_orchestration(publication_env, kind, mode):
    env, create, comment, review = publication_env
    args = ["outbox", "apply", "feature", f"{kind}/001.json"]
    if mode == "yes":
        args += ["-y"]
    elif mode == "dry-run":
        args += ["--dry-run"]
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    if mode != "yes":
        create.assert_not_called()
        comment.assert_not_called()
        review.assert_not_called()
        env[1].exec.assert_not_called()
        assert not list(env[5].root.rglob("*.json"))
    elif kind == "issue":
        create.assert_called_once()
        assert [call.args[2] for call in comment.call_args_list] == [73, 42]
        assert "fully applied" in result.output
    else:
        review.assert_called_once()
    if mode == "yes":
        removed = next(
            call.args[1] for call in env[1].exec.call_args_list if call.args[1][0] == "rm"
        )
        assert any(n.endswith("/001.json") for n in removed)
        assert not any(n.endswith("/002.json") for n in removed)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_domain_validation_exit_two(publication_env, mocker, kind):
    env, create, comment, review = publication_env
    mocker.patch("jailbee.git.get_remote_url", return_value="https://example.org/not-github.git")
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json", "-y"])
    assert result.exit_code == 2, result.output
    create.assert_not_called()
    comment.assert_not_called()
    review.assert_not_called()
    env[1].exec.assert_not_called()


def test_apply_pr_execution_read_error_is_one(publication_env, mocker):
    from jailbee import pr

    mocker.patch.object(pr, "resolve_pr", side_effect=pr.PrError("GitHub read failed"))
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", "pr/001.json", "-y"])
    assert result.exit_code == 1
    assert "GitHub read failed" in result.output
    publication_env[3].assert_not_called()


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_apply_confirmation_rechecks_scope(publication_env, mocker, kind):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    scope = mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session", return_value=RemoteRepoScope(frozenset())
    )

    def confirm(*args, **kwargs):
        scope.return_value = RemoteRepoScope(frozenset({"acme"}))
        return True

    mocker.patch("typer.confirm", side_effect=confirm)
    result = CliRunner().invoke(app, ["outbox", "apply", "feature", f"{kind}/001.json"])
    assert result.exit_code == 2, result.output
    for mutation in publication_env[1:]:
        mutation.assert_not_called()
    publication_env[0][1].exec.assert_not_called()


@pytest.mark.parametrize("name", ["ls", "show", "drop", "apply", "browse"])
def test_colliding_container_uses_public_browse_leaf(env, name):
    env[6]["name"] = f"acme-{name}"
    env[1].exists.side_effect = lambda value: value == env[6]["name"]
    result = CliRunner().invoke(app, ["outbox", "browse", name])
    assert result.exit_code == 0, result.output
    assert f"acme-{name}" in result.output
