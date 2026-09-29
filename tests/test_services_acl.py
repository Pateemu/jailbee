"""Host-global service ACL lifecycle."""

from unittest.mock import MagicMock

import yaml

from jailbee.network import SERVICES_ACL
from jailbee.services_acl import ensure_services_acl, set_services_endpoint


def test_ensure_creates_empty_acl_once():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False

    ensure_services_acl(incus)

    incus.network_acl_create.assert_called_once_with(SERVICES_ACL)
    written = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert written["egress"] == []


def test_ensure_is_noop_when_present():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True

    ensure_services_acl(incus)

    incus.network_acl_create.assert_not_called()
    incus.network_acl_set_yaml.assert_not_called()


def test_set_endpoint_writes_rules():
    incus = MagicMock()
    incus.network_acl_exists.return_value = True

    set_services_endpoint(incus, ("10.9.0.3", [4100]))

    name, body = incus.network_acl_set_yaml.call_args.args
    assert name == SERVICES_ACL
    assert yaml.safe_load(body)["egress"][0]["destination"] == "10.9.0.3/32"


def test_set_endpoint_creates_missing_acl_before_writing_endpoint():
    incus = MagicMock()
    incus.network_acl_exists.return_value = False

    set_services_endpoint(incus, ("10.9.0.3", [4100]))

    calls = incus.mock_calls
    create = next(
        i for i, call in enumerate(calls)
        if call[0] == "network_acl_create" and call.args == (SERVICES_ACL,)
    )
    writes = [i for i, call in enumerate(calls) if call[0] == "network_acl_set_yaml"]
    assert create < writes[0] < writes[1]
    assert yaml.safe_load(calls[writes[-1]].args[1])["egress"][0]["destination_port"] == "4100"
