"""Host-global ACL for JailBee service containers.

Strict NICs and both managed bridges reference this name. Create it before
writing any reference, including paths reached without a preceding apply on
an upgraded host (egress refresh, mode switches and snapshot restores).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.network import SERVICES_ACL, services_acl_yaml

if TYPE_CHECKING:
    from jailbee.incus import Incus


def ensure_services_acl(incus: Incus) -> None:
    """Create an empty services ACL if this host has not seen it yet."""
    if incus.network_acl_exists(SERVICES_ACL):
        return
    incus.network_acl_create(SERVICES_ACL)
    incus.network_acl_set_yaml(SERVICES_ACL, services_acl_yaml(None))


def set_services_endpoint(incus: Incus, endpoint: tuple[str, list[int]] | None) -> None:
    """Replace service egress grants with the current endpoint."""
    ensure_services_acl(incus)
    incus.network_acl_set_yaml(SERVICES_ACL, services_acl_yaml(endpoint))
