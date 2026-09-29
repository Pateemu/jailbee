from unittest.mock import MagicMock

from jailbee.loose_bridge import loose_bridge_host_ip


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


def test_out_of_range_or_negative_index():
    incus = MagicMock()
    incus.network_get.return_value = "10.79.115.1/30"
    assert loose_bridge_host_ip(incus, 1) is None
    assert loose_bridge_host_ip(incus, -1) is None
