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
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jailbee.paths import xdg_data_home
from jailbee.tui import warn

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
        # Framed on what jailbee needs, not on where the user should go. Under
        # `ssh -X` the user is already on the right machine — it is the session
        # that lacks a local socket — so any "run this elsewhere" instruction
        # sends them in circles. Naming the mechanism lets both the SSH case and
        # a genuinely remote display read correctly from one message.
        raise X11Unavailable(
            f"DISPLAY={raw!r} is a TCP display. jailbee passes a display into a "
            f"container by bind-mounting its socket file ({X11_SOCKET_DIR}/X<n>), "
            f"and a TCP display has none — an SSH X11 forward included, even "
            f"though jailbee is running on the right host. Use a session that "
            f"owns a local X server: a direct login or a remote-desktop session.",
        )

    num = m.group("num")
    socket_path = f"{X11_SOCKET_DIR}/X{num}"
    if not Path(socket_path).exists():
        raise X11Unavailable(
            f"DISPLAY={raw!r} parses but {socket_path} does not exist — the X "
            f"server is not listening on a local socket",
        )

    return X11Target(display=f":{num}", socket_path=socket_path)


COOKIE_MODE = 0o600
"""Owner-only. The file holds a live X authorisation cookie."""


def cookie_path(container: str) -> Path:
    """Host path of a container's generated cookie file.

    Under ``xdg_data_home()`` rather than ``cfg.shared_dir``: the shared dir is
    bind-mounted read-write and shared *between* containers, which is no place
    for auth cookies.
    """
    return xdg_data_home() / "jailbee" / "x11" / f"{container}.Xauthority"


def write_cookie(container: str, target: X11Target) -> Path | None:
    """Generate a per-container FamilyWild cookie for ``target``.

    Equivalent to the standard portable recipe::

        xauth nlist $DISPLAY | sed -e 's/^..../ffff/' | xauth -f <out> nmerge -

    run without a shell so it is testable. The wildcard family is required
    because the container's hostname differs from the host's, so a FamilyLocal
    entry keyed to the host would never be found from inside. The cookie *value*
    is unchanged, so this is not a weaker credential — and only this display's
    entry is copied, unlike bind-mounting the host's whole ``$XAUTHORITY``.

    Returns ``None`` when no cookie could be produced (no ``xauth`` on the host,
    or no entry for this display). That is not fatal: on hosts whose ``xhost``
    list carries ``SI:localuser:<user>``, ``raw.idmap uid N N`` means the server
    admits the container's dev user on UID alone.
    """
    out = cookie_path(container)
    try:
        listed = subprocess.run(
            ["xauth", "nlist", target.display],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        warn(f"Could not read an X cookie for {target.display} ({e}) — relying on host UID auth.")
        return None

    wild = "".join("ffff" + line[4:] + "\n" for line in listed.splitlines() if len(line) >= 4)
    if not wild:
        warn(f"No X cookie exists for {target.display} — relying on host UID auth.")
        return None

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)  # nmerge appends; a stale entry must not survive
    try:
        subprocess.run(
            ["xauth", "-f", str(out), "nmerge", "-"],
            input=wild,
            text=True,
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        warn(f"Could not write {out} ({e}) — relying on host UID auth.")
        return None

    if out.exists():
        out.chmod(COOKIE_MODE)
    return out


def delete_cookie(container: str) -> None:
    """Remove a container's cookie file. Idempotent."""
    cookie_path(container).unlink(missing_ok=True)
