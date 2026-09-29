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


def loose_bridge_gateways(incus: Incus) -> list[str]:
    """The bridge's own addresses (IPv4 first): where dnsmasq answers DNS and DHCP.

    Empty when the bridge has no concrete address. Callers scope a service
    container's DNS allowance to these, so a compromised service cannot use
    port 53 as an open channel to arbitrary hosts.
    """
    gateways: list[str] = []
    for key in ("ipv4.address", "ipv6.address"):
        raw = incus.network_get(LOOSE_BRIDGE, key)
        if not isinstance(raw, str) or raw.strip() in ("", "none", "auto"):
            continue
        try:
            gateways.append(str(ipaddress.ip_interface(raw.strip()).ip))
        except ValueError:
            continue
    return gateways


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
