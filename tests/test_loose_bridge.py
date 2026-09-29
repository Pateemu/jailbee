from unittest.mock import MagicMock

from jailbee.loose_bridge import loose_bridge_gateways, loose_bridge_host_ip


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
