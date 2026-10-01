"""Discovery and callback boundaries resolve the target's own effective config."""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session

from jailbee.db import get_engine
from jailbee.db.models import RegisteredRepo
from jailbee.outbox import service
from jailbee.outbox.models import OutboxError
from jailbee.outbox_io import JournalStore
from tests.outbox_support import IDENTITY, issue_files, store


@pytest.fixture
def env(mocker, make_cfg, tmp_path):
    root = tmp_path / "acme"
    root.mkdir()
    cfg = make_cfg(root, container_prefix="acme")
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "profiles": ["acme-base"],
            "status": "Running",
            "config": {"user.jailbee.issue_count": "0", "user.jailbee.review_count": "0"},
        },
    ]
    incus.exists.side_effect = lambda name: any(
        r["name"] == name for r in incus.list_containers.return_value
    )
    loader = mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    reader = mocker.patch.object(
        service,
        "read_store",
        side_effect=lambda i, c, k, **kw: store(k, issue_files() if k == "issue" else {}),
    )
    mutation = mocker.patch.object(
        service, "mutate_store", side_effect=lambda *a, **kw: kw["delete_names"]
    )
    journals = JournalStore(tmp_path / "journals")
    return cfg, incus, loader, reader, mutation, journals


def register(prefix, root):
    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=str(root),
                registered_at=datetime.now(UTC),
                synthetic_config=True,
            )
        )
        session.commit()


def test_zero_probe_count_does_not_filter(env):
    from jailbee.outbox.commands import discover

    cfg, incus, _, reader, _, journals = env
    result = discover(cfg, incus, None, all_repos=False, journal_store=journals)
    assert len(result) == 1 and result[0].available
    assert str(result[0].proposals[0].id) == "issue/001.json"
    assert reader.call_count == 2


def test_all_repos_each_own_uid_and_missing_root(env, make_cfg, tmp_path):
    from jailbee.outbox.commands import discover

    cfg, incus, loader, reader, _, journals = env
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    other = make_cfg(foreign, container_prefix="other", container_user={"uid": 2345})
    register("other", foreign)
    register("gone", tmp_path / "gone")
    loader.side_effect = lambda root: other if root == foreign else cfg
    incus.list_containers.return_value += [
        {
            "name": "other-feature",
            "profiles": ["other-base"],
            "status": "Running",
            "created_at": IDENTITY.created_at,
        },
        {
            "name": "gone-feature",
            "profiles": ["gone-base"],
            "status": "Running",
            "created_at": IDENTITY.created_at,
        },
    ]
    result = discover(cfg, incus, None, all_repos=True, journal_store=journals)
    assert [v.available for v in result] == [True, True, False]
    assert "root" in result[2].error
    assert {c.kwargs["uid"] for c in reader.call_args_list if c.args[1] == "other-feature"} == {
        2345
    }


def test_absent_config_file_accepts_registered_effective_config(env, make_cfg, tmp_path):
    from jailbee.outbox.commands import resolve_target

    cfg, incus, loader, _, _, _ = env
    foreign = tmp_path / "scratch"
    foreign.mkdir()
    other = make_cfg(foreign, container_prefix="other", container_user={"uid": 2345})
    register("other", foreign)
    loader.side_effect = lambda root: other if root == foreign else cfg
    incus.list_containers.return_value = [
        {"name": "other-feature", "profiles": ["other-base"], "status": "Running"}
    ]
    target_cfg, full = resolve_target(cfg, incus, "other-feature")
    assert target_cfg is other and full == "other-feature"
    assert not (foreign / ".jailbee/config.yaml").exists()


def test_config_load_error_never_falls_back(env, tmp_path):
    from jailbee.config import ConfigError
    from jailbee.outbox.commands import discover, resolve_target

    cfg, incus, loader, reader, _, journals = env
    root = tmp_path / "bad"
    root.mkdir()
    register("bad", root)
    incus.list_containers.return_value = [
        {"name": "bad-feature", "profiles": ["bad-base"], "status": "Running"}
    ]
    loader.side_effect = ConfigError("invalid config")
    result = discover(cfg, incus, None, all_repos=True, journal_store=journals)
    assert not result[0].available and "invalid config" in result[0].error
    with pytest.raises(OutboxError, match="invalid config"):
        resolve_target(cfg, incus, "bad-feature")
    reader.assert_not_called()


def test_scope_filters_before_any_read(env, mocker):
    from jailbee.outbox.commands import discover, resolve_target
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    cfg, incus, _, reader, _, journals = env
    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset({"acme"})),
    )
    assert discover(cfg, incus, None, all_repos=True, journal_store=journals) == ()
    with pytest.raises(OutboxError):
        resolve_target(cfg, incus, "feature")
    reader.assert_not_called()


def test_mutation_callback_rechecks_scope_after_confirmation(env, mocker):
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeleteSelection
    from jailbee.outbox.models import ProposalId
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    cfg, incus, _, _, mutation, journals = env
    scope = mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session", return_value=RemoteRepoScope(frozenset())
    )

    def confirm(plan):
        scope.return_value = RemoteRepoScope(frozenset({"acme"}))
        return True

    with pytest.raises(OutboxError):
        drop_selected(
            cfg,
            incus,
            "feature",
            ProposalId("issue", "001.json"),
            selection=DeleteSelection(),
            journal_store=journals,
            confirm=confirm,
        )
    mutation.assert_not_called()


def test_mutation_callback_reloads_target_config(env):
    from jailbee.outbox.commands import drop_selected
    from jailbee.outbox.delete import DeleteSelection
    from jailbee.outbox.models import ProposalId

    cfg, incus, loader, _, mutation, journals = env
    changed = cfg.model_copy(
        update={"container_user": cfg.container_user.model_copy(update={"uid": 2345})}
    )

    def confirm(plan):
        loader.return_value = changed
        return True

    with pytest.raises(OutboxError, match=r"config.*changed"):
        drop_selected(
            cfg,
            incus,
            "feature",
            ProposalId("issue", "001.json"),
            selection=DeleteSelection(),
            journal_store=journals,
            confirm=confirm,
        )
    mutation.assert_not_called()
