from unittest.mock import MagicMock

from jailbee.loose_bridge import (
    LOOSE_BASELINE_ACL,
    ensure_loose_bridge_acl,
    loose_bridge_gateways,
    loose_bridge_host_ip,
)


def test_index_skips_gateway():
    incus = MagicMock()
    incus.network_get.return_value = "10.79.115.1/24"
    assert loose_bridge_host_ip(incus, 0) == "10.79.115.2"
    assert loose_bridge_host_ip(incus, 1) == "10.79.115.3"


def test_none_for_auto_or_garbage():
    incus = MagicMock()
    for raw in ("auto", "none", "", "bogus"):
        incus.network_get.return_value = raw
        assert loose_bridge_host_ip(incus, 1) is None


def test_gateways_are_the_bridge_own_addresses_v4_first():
    incus = MagicMock()
    incus.network_get.side_effect = lambda _net, key: {
        "ipv4.address": "10.79.115.1/24",
        "ipv6.address": "fd42:79:115::1/64",
    }[key]
    assert loose_bridge_gateways(incus) == ["10.79.115.1", "fd42:79:115::1"]


def test_gateways_skip_unset_families():
    incus = MagicMock()
    incus.network_get.side_effect = lambda _net, key: {
        "ipv4.address": "10.79.115.1/24",
        "ipv6.address": "none",
    }[key]
    assert loose_bridge_gateways(incus) == ["10.79.115.1"]
    incus.network_get.side_effect = None
    for raw in ("auto", "none", "", "bogus"):
        incus.network_get.return_value = raw
        assert loose_bridge_gateways(incus) == []


def test_out_of_range_or_negative_index():
    incus = MagicMock()
    incus.network_get.return_value = "10.79.115.1/30"
    assert loose_bridge_host_ip(incus, 1) is None
    assert loose_bridge_host_ip(incus, -1) is None


def _bridge(config: dict[str, str], *, acl_exists: bool = True) -> MagicMock:
    incus = MagicMock()
    incus.network_acl_exists.return_value = acl_exists
    incus.network_get.side_effect = lambda _net, key: config.get(key, "")
    return incus


def test_baseline_acl_sets_allow_defaults_before_attaching():
    """Attached first, a reject default would cut every loose container off."""
    incus = _bridge({})
    assert ensure_loose_bridge_acl(incus) is True
    sets = [c.args[1:] for c in incus.network_set.call_args_list]
    assert sets == [
        ("security.acls.default.egress.action", "allow"),
        ("security.acls.default.ingress.action", "allow"),
        ("security.acls", LOOSE_BASELINE_ACL),
    ]
    incus.network_acl_create.assert_not_called()


def test_baseline_acl_is_created_when_missing():
    incus = _bridge({}, acl_exists=False)
    ensure_loose_bridge_acl(incus)
    incus.network_acl_create.assert_called_once_with(LOOSE_BASELINE_ACL)


def test_baseline_acl_keeps_acls_the_user_attached():
    incus = _bridge({"security.acls": "mine"})
    ensure_loose_bridge_acl(incus)
    assert incus.network_set.call_args.args[1:] == ("security.acls", f"mine,{LOOSE_BASELINE_ACL}")


def test_baseline_acl_is_idempotent():
    incus = _bridge({"security.acls": LOOSE_BASELINE_ACL})
    assert ensure_loose_bridge_acl(incus) is False
    incus.network_set.assert_not_called()
