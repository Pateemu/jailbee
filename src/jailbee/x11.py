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
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.paths import xdg_data_home
from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

X11_SOCKET_DIR = "/tmp/.X11-unix"

# `[host]:display[.screen]`, with the `unix` and `unix/` host forms treated as
# local. Anything else in the host position is a TCP display.
_DISPLAY_RE = re.compile(r"^(?P<host>[^:]*):(?P<num>\d+)(?:\.\d+)?$")
_LOCAL_HOSTS = frozenset({"", "unix", "unix/"})


class X11UnavailableError(Exception):
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

    Raises ``X11UnavailableError`` with an actionable message when there is nothing
    to pass. Deliberately independent of ``gui.host_is_wayland()``: a Wayland
    host running Xwayland satisfies both, and granting X11 there to run
    X11-only applications is supported rather than special-cased.
    """
    environ = os.environ if env is None else env
    raw = environ.get("DISPLAY", "").strip()
    if not raw:
        raise X11UnavailableError("DISPLAY is not set — this is not an X11 session")

    m = _DISPLAY_RE.match(raw)
    if not m:
        raise X11UnavailableError(f"DISPLAY={raw!r} could not be parsed as an X display")

    host = m.group("host")
    if host not in _LOCAL_HOSTS:
        # Framed on what jailbee needs, not on where the user should go. Under
        # `ssh -X` the user is already on the right machine — it is the session
        # that lacks a local socket — so any "run this elsewhere" instruction
        # sends them in circles. Naming the mechanism lets both the SSH case and
        # a genuinely remote display read correctly from one message.
        raise X11UnavailableError(
            f"DISPLAY={raw!r} is a TCP display. jailbee passes a display into a "
            f"container by bind-mounting its socket file ({X11_SOCKET_DIR}/X<n>), "
            f"and a TCP display has none — an SSH X11 forward included, even "
            f"though jailbee is running on the right host. Use a session that "
            f"owns a local X server: a direct login or a remote-desktop session.",
        )

    num = m.group("num")
    socket_path = f"{X11_SOCKET_DIR}/X{num}"
    if not Path(socket_path).exists():
        raise X11UnavailableError(
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

    One divergence from the ``sed`` recipe: ``sed -e 's/^..../ffff/'`` leaves a
    line shorter than 4 characters untouched and still emits it, where this
    drops it instead. Real ``xauth nlist`` output always zero-pads the family
    field to 4 hex digits, so a short line is unreachable in practice —
    recorded here as considered and dismissed, not overlooked.

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
    # nmerge appends onto an existing file, so this guarantees a fresh start —
    # not just so no stale entry survives, but so nmerge cannot inherit a
    # leftover file's mode either (the chmod below still runs regardless).
    out.unlink(missing_ok=True)
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
        # Stock xauth already creates new authority files at 0600 regardless of
        # umask, so this isn't closing a live race against it — it's
        # defence-in-depth across xauth implementations and platforms, per the
        # binding 0600 constraint on COOKIE_MODE above.
        out.chmod(COOKIE_MODE)
    return out


def delete_cookie(container: str) -> None:
    """Remove a container's cookie file. Idempotent."""
    cookie_path(container).unlink(missing_ok=True)


SOCKET_DEVICE = "x11-socket"
AUTH_DEVICE = "x11-auth"
WINDOW_LABEL = "user.jailbee.x11_window_until"
SESSION_LABEL = "user.jailbee.x11_until"
DISPLAY_ENV = "environment.DISPLAY"
XAUTHORITY_ENV = "environment.XAUTHORITY"


def container_auth_path(cfg: Config) -> str:
    """In-container path of the mounted cookie.

    Under ``/run/user/<uid>`` because that is the logind-provisioned tmpfs the
    existing socket devices already use: ownership and lifetime are understood,
    and the file disappears when the container reboots.
    """
    return f"/run/user/{cfg.container_user.uid}/jailbee-Xauthority"


@dataclass(frozen=True)
class GrantState:
    """What the container's labels say about its grant.

    ``has_labels`` is True whenever either label carries a value, including one
    that failed to parse — the sweeper needs to distinguish "nothing to do" from
    "labels to clean up".
    """

    window_until: datetime | None
    session_until: datetime | None
    has_labels: bool


def grant_state(incus: Incus, container: str) -> GrantState:
    """Read both deadline labels. Unparseable values become None."""
    raw_window = incus.config_get(container, WINDOW_LABEL)
    raw_session = incus.config_get(container, SESSION_LABEL)
    return GrantState(
        window_until=_parse_label(raw_window),
        session_until=_parse_label(raw_session),
        has_labels=bool(raw_window) or bool(raw_session),
    )


def _parse_label(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def grant(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    window: timedelta | None,
    after: timedelta | None,
    now: datetime,
) -> datetime | None:
    """Open X11 access to ``container``. Returns the door deadline, or None.

    Both deadlines are rewritten on every call. That is safe because nothing
    calls this without an explicit human act — an explicit ``jailbee x11 grant``
    or an answered prompt. Nothing opens the door automatically, so extending
    the session cannot happen behind the user's back; an abandoned container is
    still evicted ``after`` its last answer.

    ``window=None``/``after=None`` is ``--no-revert``: devices are attached and
    no label expires them.
    """
    target = resolve_target()
    cookie = write_cookie(container, target)

    _add_device(
        incus,
        container,
        SOCKET_DEVICE,
        {"source": target.socket_path, "path": target.socket_path},
    )
    if cookie is not None:
        auth_path = container_auth_path(cfg)
        _add_device(
            incus,
            container,
            AUTH_DEVICE,
            {"source": str(cookie), "path": auth_path, "readonly": "true"},
        )
        incus.config_set(container, XAUTHORITY_ENV, auth_path)
    incus.config_set(container, DISPLAY_ENV, target.display)

    door = now + window if window is not None else None
    if door is not None:
        incus.config_set(container, WINDOW_LABEL, door.isoformat())
    else:
        incus.config_unset(container, WINDOW_LABEL)
    if after is not None:
        incus.config_set(container, SESSION_LABEL, (now + after).isoformat())
    else:
        incus.config_unset(container, SESSION_LABEL)
    return door


def _add_device(
    incus: Incus,
    container: str,
    device: str,
    properties: dict[str, str],
) -> None:
    """Attach a device, tolerating one that is already there.

    Re-granting over a live grant is normal, and the existing mount is the right
    one — the source path cannot have changed without DISPLAY changing.
    """
    try:
        incus.config_device_add(container, device, "disk", properties)
    except IncusError as e:
        if "already exists" not in str(e).lower():
            raise


def _detach(incus: Incus, container: str, keys: tuple[str, ...]) -> None:
    """Remove both display devices, unset ``keys``, and drop the cookie file.

    Shared by ``revoke`` and the sweeper's door-close (``x11._close_door``),
    which differ only in whether the session label goes with it. Tolerates
    devices that are already absent — that is the steady state for a container
    that never had a grant, and for a door that closed before this call.
    """
    for device in (SOCKET_DEVICE, AUTH_DEVICE):
        try:
            incus.config_device_remove(container, device)
        except IncusError as e:
            msg = str(e).lower()
            if "doesn't exist" not in msg and "not found" not in msg:
                raise
    for key in keys:
        incus.config_unset(container, key)
    delete_cookie(container)


def revoke(cfg: Config, incus: Incus, container: str) -> None:
    """Close the door and end the session: devices, env, both labels, cookie.

    Idempotent — the no-grant steady state must not raise. Does not evict: see
    ``evict``. ``cfg`` is unused today and kept for symmetry with ``grant``.
    """
    _ = cfg
    _detach(incus, container, (DISPLAY_ENV, XAUTHORITY_ENV, WINDOW_LABEL, SESSION_LABEL))


MARKER = "JAILBEE_X11"
MARKER_VALUE = "1"
EVICT_GRACE_S = 20.0
"""Seconds between SIGTERM and SIGKILL.

Generous on purpose: a JetBrains IDE flushes indices and saves editor state on
SIGTERM, and Chrome persists session state. Killing either mid-write is how a
user loses work.
"""

# Grep every process's environment for the marker, then extract the PIDs.
#
# `-z` is load-bearing, not tidiness: it makes NUL the record separator, so
# `^`/`$` anchor to one `key=value` entry. Without it grep sees `environ` as a
# single newline-free line and does a raw substring search — which matches a
# *different* variable whose name merely ends in the marker (`MY_JAILBEE_X11=1`)
# and any variable whose *value* happens to contain the literal text. Either
# false positive gets an unrelated process SIGTERMed and then SIGKILLed.
#
# `-l` prints the filename, `-Z` NUL-terminates that filename so the `tr`
# conversion is unambiguous, and the `sed` pulls the PID out of the path.
_MARKED_PIDS_SH = (
    f"grep -lZz '^{MARKER}={MARKER_VALUE}$' /proc/[0-9]*/environ 2>/dev/null "
    "| tr '\\0' '\\n' | sed -n 's#^/proc/\\([0-9]*\\)/environ$#\\1#p'"
)


def marked_pids(incus: Incus, container: str) -> list[int]:
    """PIDs inside ``container`` carrying the grant marker.

    Set only by ``gui._gui_env``, so this is exactly the set of GUI processes
    jailbee launched under a grant — children included, since they inherit the
    environment. A ``jailbee shell`` session and the user's own dev server never
    carry it and are never touched.

    Doubles as the liveness signal: an empty list means the grant is unused, so
    the door can close early rather than waiting out the window.
    """
    try:
        out = incus.exec(container, ["bash", "-c", _MARKED_PIDS_SH])
    except IncusError:
        # Stopped or mid-destroy: nothing is running, which is the honest answer.
        return []
    pids: list[int] = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            pids.append(int(stripped))
    return pids


def signal_pids(incus: Incus, container: str, pids: list[int], signal: str) -> None:
    """Send ``signal`` (e.g. ``TERM``, ``KILL``) to ``pids``. Best effort."""
    if not pids:
        return
    args = " ".join(str(p) for p in pids)
    try:
        incus.exec(container, ["bash", "-c", f"kill -{signal} {args} 2>/dev/null || true"])
    except IncusError:
        pass


def evict(
    incus: Incus,
    containers: list[str],
    *,
    sleep_fn: object = time.sleep,
) -> dict[str, list[int]]:
    """Evict marked GUI processes from every container in one pass.

    The grace period is waited **once for the whole pass**, not per container:
    SIGTERM goes out everywhere first, then a single wait, then SIGKILL to
    whatever survived. Evicting N containers therefore costs ``EVICT_GRACE_S``,
    not N x that, which keeps a pass comfortably inside the 60 s timer tick.

    Returns the pids that were *targeted* per container (empty dict when
    nothing was marked) — captured before either signal is sent, not a
    confirmation that TERM or KILL actually landed. A container whose
    ``incus.exec`` fails transiently on both passes still shows up here;
    ``signal_pids`` is best-effort and does not report per-pid outcomes.
    ``sleep_fn`` is for test injection only.
    """
    targeted = {c: marked_pids(incus, c) for c in containers}
    targeted = {c: pids for c, pids in targeted.items() if pids}
    if not targeted:
        return {}

    for container, pids in targeted.items():
        signal_pids(incus, container, pids, "TERM")

    sleep_fn(EVICT_GRACE_S)  # type: ignore[operator]  # typed object for injection, mirrors runtime_mounts._wait_for_logind_runtime_dir

    for container in targeted:
        survivors = marked_pids(incus, container)
        signal_pids(incus, container, survivors, "KILL")

    return targeted
