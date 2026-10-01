"""Mark the processes a remote SSH session starts, and recognise them later.

Every child the SSH server starts (`pty.py`) gets `REMOTE_SESSION_ENV` in its
environment, and so does everything that child starts in turn — dashboard
actions, console commands, background workers. A command that behaves
differently for a remote caller asks `is_remote_session()` rather than taking
a flag, because a flag has to be threaded through every re-exec by hand and
the one that is forgotten fails open.

The marker only ever takes capability away, so a local user who sets it by
hand restricts their own session and nothing else. The SSH client cannot
remove it: client environment requests never reach the child (see
`server.handle_process`).

`remote.ssh.restrict_host: false` is the one way to leave it off, and only
for a server that is not itself running inside a restricted session: a
marker already in the server's own environment is inherited, never removed,
so a server started from a restricted session cannot lift the restriction
it runs under (see `host_restricted`).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REMOTE_SESSION_ENV = "JAILBEE_REMOTE_SSH"
# Set for every child of the SSH server, restricted or not. It says only
# that the person at the other end is not at this host's keyboard — which
# decides what is pointless to offer (a Qt window, the setup steps, the
# server's own working directory as a repo) rather than what is forbidden.
SSH_SESSION_ENV = "JAILBEE_SSH_SESSION"
SSH_EXCLUDED_REPOS_ENV = "JAILBEE_SSH_EXCLUDED_REPOS"
# Set (to the SSH server's port) for a child of a server whose
# `remote.ssh.gui` is on, and only then: `child_environment` removes any value
# it inherits. It says GUI apps launched here belong on the shared RDP display
# and tells the child which port to print in the connection recipe.
SSH_GUI_ENV = "JAILBEE_SSH_GUI"
# The authenticating key's fingerprint, so a launch can record a forwarding
# grant for exactly that key (see `remote_ssh.display_grants`).
SSH_KEY_FP_ENV = "JAILBEE_SSH_KEY_FP"


def child_environment(
    base: Mapping[str, str],
    *,
    term: str | None = None,
    restricted: bool = True,
    excluded_repos: Sequence[str] = (),
    gui_port: int | None = None,
    fingerprint: str | None = None,
) -> dict[str, str]:
    """The environment for a child of the SSH server, built from ``base``.

    Every child carries `SSH_SESSION_ENV`. A ``restricted`` one is also marked
    with `REMOTE_SESSION_ENV` and gets `LESSSECURE=1`, so a `less` reached
    through any path — a pager, a tool's own paging — cannot start a shell
    (`!`), an editor (`v`) or a pipe (`|`) on the host. An unrestricted one
    otherwise gets ``base`` unchanged, the restriction marker included if
    ``base`` already carries it.
    """
    env = dict(base)
    env[SSH_SESSION_ENV] = "1"
    env[SSH_EXCLUDED_REPOS_ENV] = json.dumps(list(excluded_repos))
    env.pop(SSH_GUI_ENV, None)
    env.pop(SSH_KEY_FP_ENV, None)
    if gui_port is not None:
        env[SSH_GUI_ENV] = str(gui_port)
    if fingerprint is not None:
        env[SSH_KEY_FP_ENV] = fingerprint
    if restricted:
        env[REMOTE_SESSION_ENV] = "1"
        env["LESSSECURE"] = "1"
    if term is not None:
        env["TERM"] = term
    return env


def is_remote_session(environ: Mapping[str, str] | None = None) -> bool:
    """True when this process descends from a remote SSH session.

    Any non-empty value counts: the marker fails closed.
    """
    return bool((os.environ if environ is None else environ).get(REMOTE_SESSION_ENV))


def is_ssh_session(environ: Mapping[str, str] | None = None) -> bool:
    """True when this process descends from any SSH session, restricted or not."""
    env = os.environ if environ is None else environ
    return bool(env.get(SSH_SESSION_ENV)) or is_remote_session(env)


def shared_display_port(environ: Mapping[str, str] | None = None) -> int | None:
    """The SSH server port a GUI-enabled session was started under, or None."""
    value = (os.environ if environ is None else environ).get(SSH_GUI_ENV, "")
    return int(value) if value.isascii() and value.isdigit() else None


def is_shared_display_session(environ: Mapping[str, str] | None = None) -> bool:
    """True for an SSH session whose GUI apps belong on the shared RDP display."""
    env = os.environ if environ is None else environ
    return is_ssh_session(env) and shared_display_port(env) is not None


def session_fingerprint(environ: Mapping[str, str] | None = None) -> str | None:
    """The SSH key fingerprint this session authenticated with, if known."""
    return (os.environ if environ is None else environ).get(SSH_KEY_FP_ENV) or None


def host_tree_refusal(action: str, hint: str | None = None) -> str:
    """Why a restricted remote session may not do ``action`` to the host tree.

    A remote session moves refs only: whatever it would put into the host's
    checked-out working tree — a repo config that decides host mounts, a
    build script, a submodule's files — the host's own tools read next.
    """
    message = (
        f"{action} would change the host's checked-out working tree, which a "
        f"remote SSH session never does."
    )
    if hint:
        message += f" {hint}"
    return message + " Run it on the host itself."


def mount_container_refusal(short: str) -> str:
    """Why a restricted remote session may not enter a mount-mode container.

    `jailbee new --mount` binds the host repo read-write, `.git` included:
    inside the container is the host's own working tree, which a remote
    session never writes (see `host_tree_refusal`).
    """
    return (
        f"'{short}' is a mount-mode container: it shares the host repo's working "
        f"tree, which a remote SSH session never reaches. Use a clone-mode "
        f"container, or enter this one on the host itself."
    )


def escalation_refusal(baseline_source: str) -> str:
    """Why a restricted remote session cannot approve a privilege widening.

    The branch-autostart gate asks the operator; over SSH the person
    answering is the remote user the gate exists to hold back, so neither
    the prompt nor `--yes` counts as an answer there.
    """
    return (
        f"The target branch's autostart config widens privileges beyond "
        f"{baseline_source}, and a remote SSH session cannot approve that, not "
        f"even with --yes. Run `jailbee new` on the host itself, or edit the "
        f"branch's .jailbee/config.yaml."
    )


def host_restricted(restrict_host: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a session under `remote.ssh.restrict_host` is held off the host.

    True when the setting says so, and also whenever this process already
    runs inside a restricted session, whatever the setting says.
    """
    return restrict_host or is_remote_session(environ)
