"""Unit tests for the x11 display-grant module."""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from jailbee.incus import Incus
from jailbee.x11 import X11UnavailableError, resolve_target
from tests.conftest import make_cfg


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

    with pytest.raises(X11UnavailableError, match="TCP display"):
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

    with pytest.raises(X11UnavailableError) as exc:
        resolve_target(_env("localhost:10.0"))

    message = str(exc.value)
    assert "/tmp/.X11-unix" in message  # explains the mechanism
    assert "SSH" in message  # names the case the user is actually in
    # No relocate-and-retry instruction: every phrasing of it so far has been
    # some form of "run jailbee <somewhere else>".
    assert "run jailbee" not in message.lower()


def test_missing_display_is_refused():
    with pytest.raises(X11UnavailableError, match="DISPLAY"):
        resolve_target(_env(None))


def test_empty_display_is_refused():
    with pytest.raises(X11UnavailableError, match="DISPLAY"):
        resolve_target(_env(""))


def test_unparseable_display_is_refused(mocker):
    mocker.patch("jailbee.x11.Path.exists", return_value=True)

    with pytest.raises(X11UnavailableError, match="could not be parsed"):
        resolve_target(_env(":abc"))


def test_absent_socket_is_refused(mocker):
    """The display parses but the server is not listening on a local socket."""
    mocker.patch("jailbee.x11.Path.exists", return_value=False)

    with pytest.raises(X11UnavailableError, match=re.escape("/tmp/.X11-unix/X1")):
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


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def target():
    from jailbee.x11 import X11Target

    return X11Target(":1", "/tmp/.X11-unix/X1")


def _grant_mocks(mocker, tmp_path, target, *, cookie=True):
    mocker.patch("jailbee.x11.resolve_target", return_value=target)
    mocker.patch(
        "jailbee.x11.write_cookie",
        return_value=(tmp_path / "cookie") if cookie else None,
    )
    return mocker.Mock(spec=Incus)


def test_grant_attaches_socket_and_auth_devices(tmp_path, mocker, now, target):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)

    from jailbee.x11 import grant

    grant(
        cfg, incus, "myrepo-feat-x", window=timedelta(minutes=1), after=timedelta(hours=4), now=now
    )

    added = {c.args[1]: c.args for c in incus.config_device_add.call_args_list}
    assert added["x11-socket"][2] == "disk"
    assert added["x11-socket"][3] == {
        "source": "/tmp/.X11-unix/X1",
        "path": "/tmp/.X11-unix/X1",
    }
    uid = cfg.container_user.uid
    assert added["x11-auth"][3] == {
        "source": str(tmp_path / "cookie"),
        "path": f"/run/user/{uid}/jailbee-Xauthority",
        "readonly": "true",
    }


def test_grant_sets_display_and_xauthority_per_container(tmp_path, mocker, now, target):
    """The base profile no longer carries DISPLAY, so a `jailbee shell` sees a
    correct value exactly while a grant is live and none otherwise."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)

    from jailbee.x11 import grant

    grant(
        cfg, incus, "myrepo-feat-x", window=timedelta(minutes=1), after=timedelta(hours=4), now=now
    )

    uid = cfg.container_user.uid
    sets = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert sets["environment.DISPLAY"] == ":1"
    assert sets["environment.XAUTHORITY"] == f"/run/user/{uid}/jailbee-Xauthority"


def test_grant_writes_both_deadlines(tmp_path, mocker, now, target):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)

    from jailbee.x11 import grant

    door = grant(
        cfg, incus, "myrepo-feat-x", window=timedelta(minutes=10), after=timedelta(hours=4), now=now
    )

    assert door == now + timedelta(minutes=10)
    sets = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    assert sets["user.jailbee.x11_window_until"] == (now + timedelta(minutes=10)).isoformat()
    assert sets["user.jailbee.x11_until"] == (now + timedelta(hours=4)).isoformat()


def test_grant_without_deadlines_writes_no_labels(tmp_path, mocker, now, target):
    """`--no-revert`: the devices are attached and nothing expires them."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)

    from jailbee.x11 import grant

    grant(cfg, incus, "myrepo-feat-x", window=None, after=None, now=now)

    keys = {c.args[1] for c in incus.config_set.call_args_list}
    assert "user.jailbee.x11_window_until" not in keys
    assert "user.jailbee.x11_until" not in keys
    unset = {c.args[1] for c in incus.config_unset.call_args_list}
    assert "user.jailbee.x11_window_until" in unset
    assert "user.jailbee.x11_until" in unset


def test_grant_skips_the_auth_device_when_there_is_no_cookie(tmp_path, mocker, now, target):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target, cookie=False)

    from jailbee.x11 import grant

    grant(
        cfg, incus, "myrepo-feat-x", window=timedelta(minutes=1), after=timedelta(hours=4), now=now
    )

    added = {c.args[1] for c in incus.config_device_add.call_args_list}
    assert added == {"x11-socket"}


def test_grant_tolerates_an_already_attached_device(tmp_path, mocker, now, target):
    """Re-granting over a live grant must not fail on the existing mount."""
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)
    incus.config_device_add.side_effect = IncusError("Device already exists: x11-socket")

    from jailbee.x11 import grant

    grant(
        cfg, incus, "myrepo-feat-x", window=timedelta(minutes=1), after=timedelta(hours=4), now=now
    )  # must not raise


def test_grant_propagates_other_incus_errors(tmp_path, mocker, now, target):
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = _grant_mocks(mocker, tmp_path, target)
    incus.config_device_add.side_effect = IncusError("no such container")

    from jailbee.x11 import grant

    with pytest.raises(IncusError):
        grant(
            cfg,
            incus,
            "myrepo-feat-x",
            window=timedelta(minutes=1),
            after=timedelta(hours=4),
            now=now,
        )


def test_revoke_detaches_unsets_and_deletes_the_cookie(tmp_path, mocker):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    delete = mocker.patch("jailbee.x11.delete_cookie")

    from jailbee.x11 import revoke

    revoke(cfg, incus, "myrepo-feat-x")

    removed = {c.args[1] for c in incus.config_device_remove.call_args_list}
    assert removed == {"x11-socket", "x11-auth"}
    unset = {c.args[1] for c in incus.config_unset.call_args_list}
    assert unset == {
        "environment.DISPLAY",
        "environment.XAUTHORITY",
        "user.jailbee.x11_window_until",
        "user.jailbee.x11_until",
    }
    delete.assert_called_once_with("myrepo-feat-x")


def test_revoke_tolerates_absent_devices(tmp_path, mocker):
    """Steady state for a container that never had a grant."""
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock(spec=Incus)
    incus.config_device_remove.side_effect = IncusError("Device doesn't exist")
    mocker.patch("jailbee.x11.delete_cookie")

    from jailbee.x11 import revoke

    revoke(cfg, incus, "myrepo-feat-x")  # must not raise


def test_grant_state_reads_both_labels(tmp_path, mocker, now):
    incus = mocker.Mock(spec=Incus)
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.x11_window_until": (now + timedelta(minutes=1)).isoformat(),
        "user.jailbee.x11_until": (now + timedelta(hours=4)).isoformat(),
    }.get(key)

    from jailbee.x11 import grant_state

    state = grant_state(incus, "myrepo-feat-x")

    assert state.window_until == now + timedelta(minutes=1)
    assert state.session_until == now + timedelta(hours=4)
    assert state.has_labels is True


def test_grant_state_survives_an_unparseable_label(tmp_path, mocker):
    incus = mocker.Mock(spec=Incus)
    incus.config_get.side_effect = lambda name, key: "not-a-timestamp"

    from jailbee.x11 import grant_state

    state = grant_state(incus, "myrepo-feat-x")

    assert state.window_until is None
    assert state.session_until is None
    assert state.has_labels is True  # labels exist; the sweeper must clean them


def test_marked_pids_reads_the_marker_from_proc_environ(tmp_path, mocker):
    """Children inherit the environment, so the whole Chrome/JBR tree matches —
    and a `jailbee shell` session, which never gets the marker, does not."""
    incus = mocker.Mock(spec=Incus)
    incus.exec.return_value = "412\n413\n980\n"

    from jailbee.x11 import marked_pids

    assert marked_pids(incus, "myrepo-feat-x") == [412, 413, 980]
    cmd = incus.exec.call_args.args[1]
    assert "JAILBEE_X11=1" in " ".join(cmd)
    assert "/proc" in " ".join(cmd)


def test_marked_pids_is_empty_when_nothing_matches(tmp_path, mocker):
    incus = mocker.Mock(spec=Incus)
    incus.exec.return_value = "\n"

    from jailbee.x11 import marked_pids

    assert marked_pids(incus, "myrepo-feat-x") == []


def test_marked_pids_survives_an_exec_failure(tmp_path, mocker):
    """A stopped or mid-destroy container must read as 'nothing running', not
    explode the sweeper."""
    from jailbee.incus import IncusError

    incus = mocker.Mock(spec=Incus)
    incus.exec.side_effect = IncusError("container is not running")

    from jailbee.x11 import marked_pids

    assert marked_pids(incus, "myrepo-feat-x") == []


def test_marked_pids_ignores_non_numeric_noise(tmp_path, mocker):
    incus = mocker.Mock(spec=Incus)
    incus.exec.return_value = "412\ngrep: /proc/self/environ: No such file\n980\n"

    from jailbee.x11 import marked_pids

    assert marked_pids(incus, "myrepo-feat-x") == [412, 980]


def test_marked_pids_anchors_the_pattern_to_a_whole_env_entry(mocker):
    """Without `-z` and the anchors, grep substring-matches the whole NUL-blob:
    `MY_JAILBEE_X11=1`, or any value merely containing the literal text, would
    match and get the process SIGKILLed. The shell semantics themselves are
    verified by hand (see the task report) — this pins the command so the flags
    cannot be dropped silently."""
    incus = mocker.Mock(spec=Incus)
    incus.exec.return_value = ""

    from jailbee.x11 import marked_pids

    marked_pids(incus, "myrepo-feat-x")

    script = " ".join(incus.exec.call_args.args[1])
    assert "-lZz" in script
    assert "'^JAILBEE_X11=1$'" in script


def test_evict_waits_the_grace_period_once_for_all_containers(tmp_path, mocker):
    """20 s per container would blow past the 60 s tick with three containers.
    SIGTERM everything, wait once, then SIGKILL the survivors."""
    incus = mocker.Mock(spec=Incus)
    incus.exec.side_effect = [
        "10\n11\n",  # marked_pids: container a
        "20\n",  # marked_pids: container b
        "",  # SIGTERM a
        "",  # SIGTERM b
        "10\n",  # survivors in a
        "",  # survivors in b
        "",  # SIGKILL a
    ]
    sleep_fn = mocker.Mock()

    from jailbee.x11 import EVICT_GRACE_S, evict

    targeted = evict(incus, ["a", "b"], sleep_fn=sleep_fn)

    sleep_fn.assert_called_once_with(EVICT_GRACE_S)
    assert targeted == {"a": [10, 11], "b": [20]}


def test_evict_grace_period_is_twenty_seconds():
    """A JetBrains IDE flushes indices and saves editor state on SIGTERM; a
    token couple of seconds is how a user loses their workspace."""
    from jailbee.x11 import EVICT_GRACE_S

    assert EVICT_GRACE_S == 20.0


def test_evict_does_nothing_when_no_process_is_marked(tmp_path, mocker):
    incus = mocker.Mock(spec=Incus)
    incus.exec.return_value = ""
    sleep_fn = mocker.Mock()

    from jailbee.x11 import evict

    assert evict(incus, ["a"], sleep_fn=sleep_fn) == {}
    sleep_fn.assert_not_called()


def _raw(prefix: str, name: str, *, status: str = "Running") -> dict:
    return {"name": f"{prefix}-{name}", "profiles": [f"{prefix}-base"], "status": status}


def _labels(mocker, incus, mapping):
    incus.config_get.side_effect = lambda name, key: mapping.get(key)


def test_unlabelled_container_is_left_alone(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(mocker, incus, {})

    from jailbee.x11 import check_and_revert_x11

    assert check_and_revert_x11(cfg, incus, now=now) == []
    incus.config_device_remove.assert_not_called()


def test_container_without_the_base_profile_is_skipped(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [{"name": "other", "profiles": ["default"]}]

    from jailbee.x11 import check_and_revert_x11

    assert check_and_revert_x11(cfg, incus, now=now) == []
    incus.config_get.assert_not_called()


def test_null_profiles_do_not_crash_the_sweeper(tmp_path, mocker, now):
    """A container mid-destroy is reported with "profiles": null, which bypasses
    a .get(..., []) default because the key is present with value None."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [{"name": "x", "profiles": None}]

    from jailbee.x11 import check_and_revert_x11

    assert check_and_revert_x11(cfg, incus, now=now) == []


def test_autostart_in_progress_is_skipped(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(mocker, incus, {"user.jailbee.autostart_in_progress": "1"})

    from jailbee.x11 import check_and_revert_x11

    assert check_and_revert_x11(cfg, incus, now=now) == []


def test_unexpired_door_is_left_open(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_window_until": (now + timedelta(seconds=30)).isoformat(),
            "user.jailbee.x11_until": (now + timedelta(hours=4)).isoformat(),
        },
    )
    mocker.patch("jailbee.x11.marked_pids", return_value=[42])

    from jailbee.x11 import check_and_revert_x11

    assert check_and_revert_x11(cfg, incus, now=now) == []
    incus.config_device_remove.assert_not_called()


def test_expired_door_detaches_but_keeps_the_session(tmp_path, mocker, now):
    """The door closing must not clear x11_until — the session deadline is what
    eviction later fires on."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    session = (now + timedelta(hours=4)).isoformat()
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_window_until": (now - timedelta(seconds=1)).isoformat(),
            "user.jailbee.x11_until": session,
        },
    )
    mocker.patch("jailbee.x11.marked_pids", return_value=[42])

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    assert [r.action for r in results] == ["closed"]
    removed = {c.args[1] for c in incus.config_device_remove.call_args_list}
    assert removed == {"x11-socket", "x11-auth"}
    unset = {c.args[1] for c in incus.config_unset.call_args_list}
    assert "user.jailbee.x11_window_until" in unset
    assert "user.jailbee.x11_until" not in unset


def test_unused_grant_closes_the_door_early(tmp_path, mocker, now):
    """No marked process means the grant is unused, so exposure ends now rather
    than at the end of the window."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_window_until": (now + timedelta(seconds=45)).isoformat(),
            "user.jailbee.x11_until": (now + timedelta(hours=4)).isoformat(),
        },
    )
    mocker.patch("jailbee.x11.marked_pids", return_value=[])

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    assert [r.action for r in results] == ["closed"]


def test_expired_session_evicts_and_clears_everything(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_until": (now - timedelta(seconds=1)).isoformat(),
        },
    )
    evict = mocker.patch("jailbee.x11.evict", return_value={f"{cfg.container_prefix}-feat-x": [42]})
    mocker.patch("jailbee.x11.marked_pids", return_value=[42])

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    assert [r.action for r in results] == ["evicted"]
    evict.assert_called_once()
    unset = {c.args[1] for c in incus.config_unset.call_args_list}
    assert "user.jailbee.x11_until" in unset


def test_eviction_batches_across_containers(tmp_path, mocker, now):
    """Two containers expiring on the same tick must share one grace period."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(prefix, "a"), _raw(prefix, "b")]
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_until": (now - timedelta(seconds=1)).isoformat(),
        },
    )
    evict = mocker.patch("jailbee.x11.evict", return_value={})
    mocker.patch("jailbee.x11.marked_pids", return_value=[])

    from jailbee.x11 import check_and_revert_x11

    check_and_revert_x11(cfg, incus, now=now)

    evict.assert_called_once()
    assert sorted(evict.call_args.args[1]) == [f"{prefix}-a", f"{prefix}-b"]


def test_stopped_container_with_labels_is_cleaned(tmp_path, mocker, now):
    """Devices are container config and survive a stop. Left in place they get
    mounted at the next boot *before* systemd's tmpfs lands on /tmp, shadowing
    the socket and leaving a grant that looks live but cannot work."""
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        _raw(cfg.container_prefix, "feat-x", status="Stopped"),
    ]
    _labels(
        mocker,
        incus,
        {
            "user.jailbee.x11_window_until": (now + timedelta(hours=1)).isoformat(),
            "user.jailbee.x11_until": (now + timedelta(hours=4)).isoformat(),
        },
    )

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    assert [r.action for r in results] == ["cleaned"]
    removed = {c.args[1] for c in incus.config_device_remove.call_args_list}
    assert removed == {"x11-socket", "x11-auth"}


def test_unparseable_labels_are_cleared_rather_than_retried(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(cfg.container_prefix, "feat-x")]
    _labels(mocker, incus, {"user.jailbee.x11_until": "garbage"})

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    assert [r.action for r in results] == ["cleaned"]


def test_one_broken_container_does_not_break_the_loop(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path, x11={"enabled": True})
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_raw(prefix, "a"), _raw(prefix, "b")]

    def config_get(name, key):
        if name == f"{prefix}-a":
            raise RuntimeError("boom")
        return {"user.jailbee.x11_until": (now - timedelta(seconds=1)).isoformat()}.get(key)

    incus.config_get.side_effect = config_get
    mocker.patch("jailbee.x11.evict", return_value={})
    mocker.patch("jailbee.x11.marked_pids", return_value=[])

    from jailbee.x11 import check_and_revert_x11

    results = check_and_revert_x11(cfg, incus, now=now)

    actions = {r.container: r.action for r in results}
    assert actions[f"{prefix}-b"] == "evicted"
    assert actions[f"{prefix}-a"] == "error"
    broken = next(r for r in results if r.container == f"{prefix}-a")
    assert broken.error is not None
    # The labels must survive so the next tick retries rather than losing the
    # grant's deadlines to a transient failure.
    assert incus.config_device_remove.call_count == 0
