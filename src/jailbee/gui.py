"""GUI app launchers — IDE and Chrome inside containers."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, get_args

from jailbee.config import CONTAINER_USERNAME, Config, IdeName
from jailbee.incus import Incus
from jailbee.tui import error, info

if TYPE_CHECKING:
    from datetime import datetime

    from jailbee.global_config import GlobalConfig

# Allowed JetBrains launcher names — source of truth is the IdeName Literal
# in config.py. Resolving this once at import time avoids drift.
_SUPPORTED_IDE_LAUNCHERS: frozenset[str] = frozenset(get_args(IdeName))


def _gui_env(cfg: Config) -> dict[str, str]:
    """Environment vars for GUI apps inside the container.

    HOME must be set explicitly: ``incus exec --user <uid>`` doesn't read
    /etc/passwd to derive it, so without this apps see ``HOME=`` and
    fail (Chrome can't write its profile, JetBrains can't find its
    config, etc.).
    """
    uid = cfg.container_user.uid
    return {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        # Marks this process tree as launched under an X11 grant. Children
        # inherit it, which is what lets `x11.evict` target exactly jailbee's GUI
        # processes — and never the user's shell session or dev server — and what
        # makes the liveness check possible without inspecting sockets.
        "JAILBEE_X11": "1",
    }


def host_is_wayland() -> bool:
    """Return True if the host session is Wayland-native.

    Used to pass --ozone-platform=wayland to Chrome and to decide whether
    to bind-mount the host's Wayland socket into containers (see
    ``runtime_mounts``). On X11 hosts there is no wayland-0 socket to
    mount; Chrome falls back to its auto-detect (DISPLAY) and other GUI
    apps follow suit.
    """
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def ensure_display(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    gcfg: GlobalConfig,
    now: datetime,
) -> bool:
    """Make sure ``container`` can reach a display. True when a launch may go on.

    Wayland needs nothing: the compositor socket is attached at container start.
    X11 needs an open door, and this function **never opens one on its own** —
    whenever the door is shut it asks, and without a TTY it fails with a hint.
    Because every opening carries a fresh answer, that answer also rewrites both
    deadlines.
    """
    import questionary

    from jailbee import x11
    from jailbee.config import X11_WINDOW_PRESETS, parse_x11_duration

    if host_is_wayland():
        return True

    policy = cfg.effective_x11(gcfg)
    if not policy.enabled:
        error(
            "X11 display grants are disabled. This is an X11 session, so "
            "`jailbee ide`/`jailbee chrome` need `x11.enabled: true` in "
            "~/.config/jailbee/global.yaml. See docs/security.md for what that "
            "opens up.",
        )
        return False

    state = x11.grant_state(incus, container)
    if state.window_until is not None and state.window_until > now:
        return True

    default = _format_duration(policy.window)
    if not sys.stdin.isatty():
        error(
            f"X11 access to {container} is closed\n"
            f"  hint: jailbee x11 grant {container} --window {default}",
        )
        return False

    presets = list(X11_WINDOW_PRESETS)
    if default not in presets:
        presets.insert(0, default)
    custom = "__custom__"
    cancel = "__cancel__"
    choices = [
        questionary.Choice(
            title=f"{p}  (config default)" if p == default else p,
            value=p,
        )
        for p in presets
    ]
    choices.append(questionary.Choice(title="custom…", value=custom))
    choices.append(questionary.Choice(title="cancel", value=cancel))

    answer = questionary.select(
        f"X11 access to {container} is closed. Open the window for how long?",
        choices=choices,
        default=default,
    ).ask()
    if answer is None or answer == cancel:
        return False
    if answer == custom:
        raw = questionary.text(
            "Window (e.g. 30s, 10m, 2h; max 24h):",
            default=default,
            validate=_validate_x11_duration,
        ).ask()
        if raw is None:
            return False
        answer = raw

    window = parse_x11_duration(answer)
    # One number was typed, so nothing the user said contradicts anything: keep
    # `window <= after` by moving the end they did not name. The flag path, where
    # two numbers can genuinely conflict, rejects instead.
    after = max(window, policy.after_duration())

    try:
        x11.grant(cfg, incus, container, window=window, after=after, now=now)
    except x11.X11UnavailableError as e:
        error(str(e))
        return False
    info(f"Opened X11 access to {container} (window {answer}, session {after})")
    return True


def _validate_x11_duration(raw: str) -> bool | str:
    """questionary validator: True when parseable, else the error message."""
    from jailbee.config import parse_x11_duration

    try:
        parse_x11_duration(raw)
    except ValueError as e:
        return str(e)
    return True


def _format_duration(value: str | int) -> str:
    """Render an ``X11Config`` duration field as prompt-ready text.

    The field is ``str | int``; a bare int means minutes, matching
    ``config.format_loose_after``.
    """
    return f"{value}m" if isinstance(value, int) else value


def _utcnow() -> datetime:
    """Wallclock helper, factored for test mocking (mirrors cli._now)."""
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _detached_incus_exec(
    container: str,
    uid: int,
    env_args: list[str],
    inner_cmd: str,
    log_path: str,
    *,
    cwd: str | None = None,
) -> None:
    """Spawn `incus exec` so the GUI app survives `jailbee` returning.

    Two layers of detachment: the parent Python ``subprocess.Popen`` is given
    a fresh session and ``/dev/null`` stdio so the child doesn't share jailbee's
    TTY (which would leave the terminal in a messed-up state on parent
    exit). The inner shell uses ``setsid`` + ``</dev/null`` so the GUI
    process detaches from the bash that launched it.
    """
    cwd_args = ["--cwd", cwd] if cwd else []
    shell = f"setsid bash -c {shlex.quote(inner_cmd)} </dev/null >{shlex.quote(log_path)} 2>&1 &"
    subprocess.Popen(
        [
            "incus",
            "exec",
            container,
            "--user",
            str(uid),
            *cwd_args,
            *env_args,
            "--",
            "bash",
            "-c",
            shell,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_ide(
    cfg: Config,
    incus: Incus,
    container: str,
    app: str,
    *,
    gcfg: GlobalConfig,
) -> None:
    """Launch a JetBrains IDE from /opt/jetbrains-toolbox.

    Toolbox apps live at ``/opt/jetbrains-toolbox/apps/<app-id>/bin/<launcher>``.
    The <app-id> can vary across Toolbox versions and edition flavours
    (e.g. ``intellij-idea-ultimate``, ``pycharm-professional``), but the
    launcher binary is always the IDE's short name — so we match by launcher
    name, not by app-id.
    """
    if app not in _SUPPORTED_IDE_LAUNCHERS:
        supported = ", ".join(sorted(_SUPPORTED_IDE_LAUNCHERS))
        error(f"Unknown IDE app: {app} (must be one of: {supported})")
        return

    if not ensure_display(cfg, incus, container, gcfg=gcfg, now=_utcnow()):
        return

    find_cmd = (
        f"find /opt/jetbrains-toolbox/apps -maxdepth 4 -type f -name '{app}' -executable | head -1"
    )
    result = incus.exec(
        container,
        ["bash", "-c", find_cmd],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
    ).strip()

    if not result:
        error(f"No {app} launcher found in /opt/jetbrains-toolbox/apps")
        return

    from jailbee.lifecycle import container_repo_dir

    repo_dir = container_repo_dir(cfg, incus, container)
    log_path = f"/tmp/jailbee-ide-{app}.log"
    info(f"Launching {app} in {container} (background, logs in container: {log_path})")
    env_args: list[str] = []
    for k, v in _gui_env(cfg).items():
        env_args += ["--env", f"{k}={v}"]
    _detached_incus_exec(
        container,
        cfg.container_user.uid,
        env_args,
        f"{shlex.quote(result)} {shlex.quote(repo_dir)}",
        log_path,
        cwd=repo_dir,
    )


def open_chrome(
    cfg: Config,
    incus: Incus,
    container: str,
    url: str | None,
    *,
    gcfg: GlobalConfig,
) -> None:
    """Launch Chrome inside the container, optionally to a URL.

    Allocates a slot from the chrome profile pool before
    launching, so each container has its own Chrome profile dir and
    they don't collide on Chrome's SingletonLock.
    """
    if not ensure_display(cfg, incus, container, gcfg=gcfg, now=_utcnow()):
        return

    from jailbee.chrome_pool import allocate as chrome_pool_allocate

    chrome_pool_allocate(cfg, incus, container)

    args = ["/opt/google/chrome/google-chrome"]
    if host_is_wayland():
        # Chrome 2025 defaults to X11 even with WAYLAND_DISPLAY set; force
        # the Ozone Wayland backend explicitly.
        args.append("--ozone-platform=wayland")
    if cfg.chrome.dark_mode:
        args += ["--force-dark-mode", "--enable-features=WebContentsForceDark"]
    if url:
        args.append(url)

    log_path = "/tmp/jailbee-chrome.log"
    info(f"Launching Chrome in {container} (background, logs in container: {log_path})")
    env_args: list[str] = []
    for k, v in _gui_env(cfg).items():
        env_args += ["--env", f"{k}={v}"]
    _detached_incus_exec(
        container,
        cfg.container_user.uid,
        env_args,
        " ".join(shlex.quote(a) for a in args),
        log_path,
    )
