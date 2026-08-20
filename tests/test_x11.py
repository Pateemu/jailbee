"""Unit tests for the x11 display-grant module."""

from __future__ import annotations

import subprocess

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


def test_cookie_path_is_per_container_under_xdg_data_home(tmp_path, mocker):
    """Not under cfg.shared_dir: that directory is bind-mounted read-write and
    shared *between* containers, which is no place for auth cookies."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)

    from jailbee.x11 import cookie_path

    assert cookie_path("myrepo-feat-x") == tmp_path / "jailbee/x11/myrepo-feat-x.Xauthority"


def test_write_cookie_rewrites_the_family_to_wild(tmp_path, mocker):
    """The container's hostname differs from the host's, so a FamilyLocal entry
    keyed to the host would not be found. Rewriting the first field to `ffff`
    (FamilyWild) is the portable form and carries the same cookie value."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)
    run = mocker.patch("jailbee.x11.subprocess.run")
    run.side_effect = [
        mocker.Mock(stdout="0100 0002 7470 0000  0012 4d49 0010 deadbeef\n", returncode=0),
        mocker.Mock(stdout="", returncode=0),
    ]

    from jailbee.x11 import X11Target, write_cookie

    path = write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1"))

    assert path == tmp_path / "jailbee/x11/myrepo-feat-x.Xauthority"
    nlist_cmd = run.call_args_list[0].args[0]
    assert nlist_cmd == ["xauth", "nlist", ":1"]
    merge_call = run.call_args_list[1]
    assert merge_call.args[0] == ["xauth", "-f", str(path), "nmerge", "-"]
    # Full equality, not `startswith("ffff ")`: a prefix check passes even if
    # everything after the family field is corrupted, truncated, duplicated, or
    # missing its trailing newline — and the cookie value is the whole point.
    assert merge_call.kwargs["input"] == "ffff 0002 7470 0000  0012 4d49 0010 deadbeef\n"


def test_write_cookie_sets_owner_only_permissions(tmp_path, mocker):
    """The file holds a live X authorisation cookie, so it must not be readable
    by other users on the host."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)

    def fake_run(cmd, **kwargs):
        # Stand in for the real `xauth -f <out> nmerge -`, which creates the
        # file. Without this the chmod has nothing to act on and the assertion
        # below would pass for the wrong reason.
        if cmd[:2] == ["xauth", "-f"]:
            out = tmp_path / "jailbee/x11/myrepo-feat-x.Xauthority"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("cookie")
            out.chmod(0o644)  # deliberately wrong, so the chmod must fix it
            return mocker.Mock(stdout="")
        return mocker.Mock(stdout="0100 0002 7470 0000  0012 4d49 0010 deadbeef\n")

    mocker.patch("jailbee.x11.subprocess.run", side_effect=fake_run)

    from jailbee.x11 import X11Target, write_cookie

    path = write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1"))

    assert path is not None
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_cookie_returns_none_when_the_host_has_no_cookie(tmp_path, mocker):
    """Empty `xauth nlist` output means this display has no auth entry. The
    grant still proceeds without the auth device, because a host whose xhost
    list carries SI:localuser admits the container on UID alone."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)
    mocker.patch("jailbee.x11.subprocess.run", return_value=mocker.Mock(stdout="\n"))

    from jailbee.x11 import X11Target, write_cookie

    assert write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1")) is None


def test_write_cookie_returns_none_when_xauth_is_missing(tmp_path, mocker):
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)
    mocker.patch("jailbee.x11.subprocess.run", side_effect=FileNotFoundError("xauth"))

    from jailbee.x11 import X11Target, write_cookie

    assert write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1")) is None


def test_write_cookie_returns_none_when_xauth_nlist_exits_nonzero(tmp_path, mocker):
    """A non-zero exit is a distinct failure from a missing binary, and it is one
    of the three degraded-but-working paths the design requires."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)
    mocker.patch(
        "jailbee.x11.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["xauth", "nlist", ":1"]),
    )

    from jailbee.x11 import X11Target, write_cookie

    assert write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1")) is None


def test_write_cookie_returns_none_when_nmerge_fails(tmp_path, mocker):
    """The second subprocess call has its own failure handling, and nothing
    reached it before this test — deleting that whole try/except broke no test."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["xauth", "-f"]:
            raise subprocess.CalledProcessError(1, cmd)
        return mocker.Mock(stdout="0100 0002 7470 0000  0012 4d49 0010 deadbeef\n")

    mocker.patch("jailbee.x11.subprocess.run", side_effect=fake_run)

    from jailbee.x11 import X11Target, write_cookie

    assert write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1")) is None


def test_write_cookie_returns_none_when_nmerge_binary_disappears(tmp_path, mocker):
    """Same branch, the FileNotFoundError half — `xauth` vanishing between the
    two calls is far-fetched, but the branch handles both and both are cheap."""
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["xauth", "-f"]:
            raise FileNotFoundError("xauth")
        return mocker.Mock(stdout="0100 0002 7470 0000  0012 4d49 0010 deadbeef\n")

    mocker.patch("jailbee.x11.subprocess.run", side_effect=fake_run)

    from jailbee.x11 import X11Target, write_cookie

    assert write_cookie("myrepo-feat-x", X11Target(":1", "/tmp/.X11-unix/X1")) is None


def test_delete_cookie_is_idempotent(tmp_path, mocker):
    mocker.patch("jailbee.x11.xdg_data_home", return_value=tmp_path)

    from jailbee.x11 import cookie_path, delete_cookie

    path = cookie_path("myrepo-feat-x")
    path.parent.mkdir(parents=True)
    path.write_text("cookie")

    delete_cookie("myrepo-feat-x")
    assert not path.exists()

    delete_cookie("myrepo-feat-x")  # absent file must not raise
