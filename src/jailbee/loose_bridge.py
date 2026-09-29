"""Deterministic host addresses on the shared `jailbee-loose` bridge.

Index 0 is the registry mirror, index 1 the LiteLLM proxy. A static address
lowers to a dnsmasq reservation, so a recreated service keeps its address.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.incus import Incus

LOOSE_BRIDGE = "jailbee-loose"


def loose_bridge_host_ip(incus: Incus, index: int) -> str | None:
    if index < 0:
        return None
    raw = incus.network_get(LOOSE_BRIDGE, "ipv4.address")
    if not isinstance(raw, str):
        return None
    cidr = raw.strip()
    if not cidr or cidr in ("none", "auto"):
        return None
    try:
        iface = ipaddress.IPv4Interface(cidr)
    except ValueError:
        return None
    # Avoid materialising a potentially huge bridge subnet just to pick one host.
    for position, host in enumerate(h for h in iface.network.hosts() if h != iface.ip):
        if position == index:
            return str(host)
    return None
