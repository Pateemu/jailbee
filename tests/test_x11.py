"""Unit tests for the x11 display-grant module."""

from __future__ import annotations

import pytest

from jailbee.x11 import X11Unavailable, resolve_target


def _env(display: str | None) -> dict[str, str]:
    return {} if display is None else {"DISPLAY": display}


@pytest.mark.parametrize(
    ("display", "expected_display", "expected_socket"),
    [
        (":0", ":0", "/tmp/.X11-unix/X0"),
        (":1", ":1", "/tmp/.X11-unix/X1"),
        (":1.0", ":1", "/tmp/.X11-unix/X1"),
        ("unix/:1", ":1", "/tmp/.X11-unix/X1"),
        ("unix:1", ":1", "/tmp/.X11-unix/X1"),
        (":10", ":10", "/tmp/.X11-unix/X10"),
    ],
)
def test_local_displays_resolve_to_their_socket(mocker, display, expected_display, expected_socket):
    """The display number is canonicalised (screen suffix dropped) and mapped to
    the filesystem socket the container needs bind-mounted.

    The mock is keyed on the path, not just `return_value=True`: a bare
    `return_value=True` would also pass for a `resolve_target` that checked
    some fixed/wrong socket path and ignored the parsed display number, as
    long as it still formatted the right string into the return value.
    Keying on `self` makes the test fail unless the code actually probes
    `expected_socket`.
    """
    mocker.patch(
        "jailbee.x11.Path.exists",
        autospec=True,
        side_effect=lambda self: str(self) == expected_socket,
    )

    target = resolve_target(_env(display))

    assert target.display == expected_display
    assert target.socket_path == expected_socket


@pytest.mark.parametrize("display", ["localhost:10.0", "myhost:0", "192.168.1.5:0"])
def test_tcp_displays_are_refused(mocker, display):
    """An SSH-forwarded or remote display has no local socket to pass. Attaching
    nothing and saying why beats attaching something that cannot work."""
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    with pytest.raises(X11Unavailable, match="TCP display"):
        resolve_target(_env(display))


def test_tcp_refusal_explains_the_mechanism_and_names_the_ssh_case(mocker):
    """`ssh -X` sets DISPLAY=localhost:10.0 — forwarded over loopback on the host
    the user is already on, so the refusal must not read as "you are on the wrong
    machine". What makes it actionable is naming what jailbee needs (a socket file
    to bind-mount) and saying outright that an SSH forward cannot supply one.

    This asserts the message carries both of those, and that it does not issue a
    relocate-and-retry instruction. Whether the resulting prose is *good* is a
    review question, not something a unit test can settle — the assertions below
    are a floor, not a proof.
    """
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    with pytest.raises(X11Unavailable) as exc:
        resolve_target(_env("localhost:10.0"))

    message = str(exc.value)
    assert "/tmp/.X11-unix" in message  # explains the mechanism
    assert "SSH" in message  # names the case the user is actually in
    # No relocate-and-retry instruction: every phrasing of it so far has been
    # some form of "run jailbee <somewhere else>".
    assert "run jailbee" not in message.lower()


def test_missing_display_is_refused():
    with pytest.raises(X11Unavailable, match="DISPLAY"):
        resolve_target(_env(None))


def test_empty_display_is_refused():
    with pytest.raises(X11Unavailable, match="DISPLAY"):
        resolve_target(_env(""))


def test_unparseable_display_is_refused(mocker):
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    with pytest.raises(X11Unavailable, match="could not be parsed"):
        resolve_target(_env(":abc"))


def test_absent_socket_is_refused(mocker):
    """The display parses but the server is not listening on a local socket."""
    mocker.patch("jailbee.x11.Path.exists", return_value=False)

    with pytest.raises(X11Unavailable, match="/tmp/.X11-unix/X1"):
        resolve_target(_env(":1"))


def test_detection_does_not_consult_wayland(mocker):
    """A Wayland host running Xwayland has both. Granting X11 there is allowed
    on purpose, so detection must never branch on WAYLAND_DISPLAY."""
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    target = resolve_target({"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0"})

    assert target.display == ":1"
