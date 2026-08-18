"""X11 display grants — time-bounded passthrough of the host's X socket.

Wayland passthrough is unconditional and lives in ``runtime_mounts``: the
compositor socket is attached at container start and stays for the container's
life. X11 cannot work that way. The protocol has no client isolation, so any
container holding the server socket can read keystrokes meant for other
windows, capture any window's contents, and inject synthetic input into host
applications.

So X11 access is a *grant*: opt-in per host, attached after boot, and carrying
two deadlines. ``x11_window_until`` (the door) bounds how long new connections
can be made; ``x11_until`` (the session) bounds how long acquired ones may be
used, after which jailbee-launched GUI processes are evicted. Enforcement rides
``jailbee-net-refresh.timer`` — see ``check_and_revert_x11``.

This module calls ``subprocess`` directly for ``xauth`` on the *host*. That is
the same category as ``registry``/``gui``/``maintenance``: not an Incus
operation, so the "only incus.py shells out" rule does not apply.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

X11_SOCKET_DIR = "/tmp/.X11-unix"

# `[host]:display[.screen]`, with the `unix` and `unix/` host forms treated as
# local. Anything else in the host position is a TCP display.
_DISPLAY_RE = re.compile(r"^(?P<host>[^:]*):(?P<num>\d+)(?:\.\d+)?$")
_LOCAL_HOSTS = frozenset({"", "unix", "unix/"})


class X11Unavailable(Exception):
    """The host has no local X display that can be passed into a container."""


@dataclass(frozen=True)
class X11Target:
    """A local X display that can be bind-mounted into a container.

    ``display`` is canonicalised to ``:<n>`` (any screen suffix dropped) — it is
    what gets exported as ``DISPLAY`` inside the container and what ``xauth`` is
    queried with, so the cookie's display number matches what clients look up.
    """

    display: str
    socket_path: str


def resolve_target(env: Mapping[str, str] | None = None) -> X11Target:
    """Resolve the host's ``$DISPLAY`` into a passable target.

    Raises ``X11Unavailable`` with an actionable message when there is nothing
    to pass. Deliberately independent of ``gui.host_is_wayland()``: a Wayland
    host running Xwayland satisfies both, and granting X11 there to run
    X11-only applications is supported rather than special-cased.
    """
    environ = os.environ if env is None else env
    raw = environ.get("DISPLAY", "").strip()
    if not raw:
        raise X11Unavailable("DISPLAY is not set — this is not an X11 session")

    m = _DISPLAY_RE.match(raw)
    if not m:
        raise X11Unavailable(f"DISPLAY={raw!r} could not be parsed as an X display")

    host = m.group("host")
    if host not in _LOCAL_HOSTS:
        # Covers both a genuinely remote display and `ssh -X`'s
        # `localhost:10.0`, which is forwarded over loopback on *this* machine.
        # The message must not claim the display lives elsewhere: under SSH
        # forwarding the user is already on the right machine, and telling them
        # to move would send them in circles.
        raise X11Unavailable(
            f"DISPLAY={raw!r} is a TCP display, and only a local X socket "
            f"({X11_SOCKET_DIR}/X<n>) can be passed into a container. If this is "
            f"an SSH session with X11 forwarding, there is no local socket to "
            f"forward — run jailbee from a session on the machine running the X "
            f"server.",
        )

    num = m.group("num")
    socket_path = f"{X11_SOCKET_DIR}/X{num}"
    if not Path(socket_path).exists():
        raise X11Unavailable(
            f"DISPLAY={raw!r} parses but {socket_path} does not exist — the X "
            f"server is not listening on a local socket",
        )

    return X11Target(display=f":{num}", socket_path=socket_path)
