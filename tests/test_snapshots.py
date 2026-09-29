"""Tests for snapshot operations."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from jailbee.snapshots import (
    create_snapshot,
    delete_snapshot,
    list_snapshots,
    restore_snapshot,
    snapshot_default_tag,
)


def test_default_tag_uses_iso_date(monkeypatch):
    monkeypatch.setattr(
        "jailbee.snapshots._now",
        lambda: datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
    )
    assert snapshot_default_tag() == "snap-2026-05-05-120000Z"


def test_create_snapshot_uses_provided_tag():
    incus = MagicMock()
    tag = create_snapshot(incus, "feat-x", "before-migration")
    incus.snapshot_create.assert_called_once_with("feat-x", "before-migration")
    assert tag == "before-migration"


def test_create_snapshot_uses_default_tag_when_none(monkeypatch):
    monkeypatch.setattr(
        "jailbee.snapshots._now",
        lambda: datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
    )
    incus = MagicMock()
    tag = create_snapshot(incus, "feat-x", None)
    assert tag == "snap-2026-05-05-120000Z"


def test_restore_snapshot_calls_incus(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "myrepo")
    incus = MagicMock()
    incus.list_containers.return_value = []
    mocker.patch("jailbee.egress_scope.apply_container_acl")
    restore_snapshot(cfg, incus, "feat-x", "before-migration")
    incus.snapshot_restore.assert_called_once_with("feat-x", "before-migration")


def test_delete_snapshot_calls_incus():
    incus = MagicMock()
    delete_snapshot(incus, "feat-x", "old")
    incus.snapshot_delete.assert_called_once_with("feat-x", "old")


def test_list_snapshots_returns_incus_payload():
    incus = MagicMock()
    incus.snapshot_list.return_value = [{"name": "snap1", "created_at": "..."}]
    out = list_snapshots(incus, "feat-x")
    assert out == [{"name": "snap1", "created_at": "..."}]


def test_restore_snapshot_rematerialises_the_egress_acl(make_cfg, tmp_path, mocker):
    from jailbee.snapshots import restore_snapshot

    cfg = make_cfg(tmp_path / "myrepo")
    incus = mocker.MagicMock()
    apply_acl = mocker.patch("jailbee.egress_scope.apply_container_acl")

    restore_snapshot(cfg, incus, "myrepo-feat", "before-upgrade")

    incus.snapshot_restore.assert_called_once_with("myrepo-feat", "before-upgrade")
    assert apply_acl.call_args.args[2] == "myrepo-feat"


def test_restore_work_snapshot_never_calls_legacy_nic_mutator(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "myrepo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "myrepo-feat", "profiles": [f"{cfg.container_prefix}-net-work-strict"]}
    ]
    legacy = mocker.patch("jailbee.egress_scope.apply_container_acl")
    work = mocker.patch("jailbee.work_acl.apply_work_container_acl")
    mocker.patch("jailbee.work_acl.reconcile_work_acl")

    restore_snapshot(cfg, incus, "myrepo-feat", "before-upgrade")

    work.assert_called_once_with(cfg, incus, "myrepo-feat")
    legacy.assert_not_called()


def test_restore_strict_work_snapshot_creates_services_acl_before_nic_write(
    make_cfg, tmp_path, mocker
):
    cfg = make_cfg(tmp_path / "myrepo")
    name = "myrepo-feat"
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": name,
            "profiles": [f"{cfg.container_prefix}-net-work-strict"],
            "devices": {
                "eth0": {
                    "type": "nic",
                    "network": "jailbee-work",
                    "ipv4.address": "10.42.0.2",
                    "security.ipv4_filtering": "true",
                    "security.acls": "stale",
                }
            },
        }
    ]
    incus.config_get.return_value = None
    incus.network_get.return_value = "jailbee-work-baseline"
    incus.network_acl_exists.side_effect = lambda acl: acl != "jailbee-services"
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.work_acl.reconcile_work_acl")

    restore_snapshot(cfg, incus, name, "before-upgrade")

    assert incus.config_device_set.call_args.args[2]["security.acls"] == (
        "myrepo-allowlist,jailbee-services"
    )
    create_idx = next(
        i
        for i, call in enumerate(incus.mock_calls)
        if call[0] == "network_acl_create" and call.args == ("jailbee-services",)
    )
    set_idx = next(i for i, call in enumerate(incus.mock_calls) if call[0] == "config_device_set")
    assert create_idx < set_idx
