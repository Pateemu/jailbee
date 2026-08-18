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
def test_local_displays_resolve_to_their_socket(
    tmp_path, mocker, display, expected_display, expected_socket
):
    """The display number is canonicalised (screen suffix dropped) and mapped to
    the filesystem socket the container needs bind-mounted."""
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    target = resolve_target(_env(display))

    assert target.display == expected_display
    assert target.socket_path == expected_socket


@pytest.mark.parametrize("display", ["localhost:10.0", "myhost:0", "192.168.1.5:0"])
def test_tcp_displays_are_refused(tmp_path, mocker, display):
    """An SSH-forwarded or remote display has no local socket to pass. Attaching
    nothing and saying why beats attaching something that cannot work."""
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    with pytest.raises(X11Unavailable, match="another machine"):
        resolve_target(_env(display))


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
