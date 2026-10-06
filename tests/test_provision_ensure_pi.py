"""Behaviour of the ensure-pi.sh provision script, run in a real bash.

`npm` is a stub on PATH: `npm view` prints `$LATEST` (or fails when it is
unset), `npm install` logs the call and drops an executable `bin/pi` under its
`--prefix`, as the real package does, unless `$FAIL_INSTALL` is set.
"""

import os
import subprocess

import pytest

from jailbee.agents import _resolve_bundled

PKG = "@earendil-works/pi-coding-agent"

_NPM = """#!/bin/sh
echo "$*" >> "$NPM_LOG"
case "$1" in
view)
    [ -n "$LATEST" ] || exit 1
    echo "$LATEST"
    ;;
install)
    [ -z "$FAIL_INSTALL" ] || exit 1
    while [ "$1" != --prefix ]; do shift; done
    mkdir -p "$2/bin"
    printf '#!/bin/sh\\n' > "$2/bin/pi"
    chmod 755 "$2/bin/pi"
    ;;
esac
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def _run(tmp_path, home, *, latest="1.1.0", auto_update=True, fail_install=False):
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    (stub_bin / "npm").write_text(_NPM)
    (stub_bin / "npm").chmod(0o755)
    log = tmp_path / "npm.log"
    log.unlink(missing_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "NPM_LOG": str(log),
        "JAILBEE_AUTO_UPDATE": "true" if auto_update else "false",
    }
    if latest:
        env["LATEST"] = latest
    if fail_install:
        env["FAIL_INSTALL"] = "1"
    result = subprocess.run(
        ["bash", "-c", _resolve_bundled("__bundled__:ensure-pi.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _store(home):
    return home / ".local/share/pi"


def _seed(home, *versions, current):
    for v in versions:
        binary = _store(home) / "releases" / v / "bin/pi"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    (_store(home) / "current").symlink_to(f"releases/{current}")


def _linked_release(home):
    return (home / ".local/bin/pi").resolve().parent.parent.name


def test_empty_store_installs_the_latest_release_even_with_auto_update_off(tmp_path, home):
    result, calls = _run(tmp_path, home, auto_update=False)

    assert result.returncode == 0, result.stderr
    assert calls[0] == f"view {PKG} version"
    assert "--ignore-scripts" in calls[1]
    assert calls[1].endswith(f" {PKG}@1.1.0")
    assert _linked_release(home) == "1.1.0"


def test_populated_store_with_auto_update_off_only_links(tmp_path, home):
    """A second container of the repo must not touch the registry at all."""
    _seed(home, "1.0.3", current="1.0.3")

    result, calls = _run(tmp_path, home, auto_update=False)

    assert result.returncode == 0, result.stderr
    assert calls == []
    assert _linked_release(home) == "1.0.3"


def test_up_to_date_store_does_not_reinstall(tmp_path, home):
    _seed(home, "1.1.0", current="1.1.0")

    result, calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert calls == [f"view {PKG} version"]


def test_update_installs_beside_the_running_release(tmp_path, home):
    """A sibling container's pi is still loading chunks from 1.0.3."""
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _linked_release(home) == "1.1.0"
    assert (_store(home) / "releases/1.0.3/bin/pi").exists()


def test_update_keeps_the_two_newest_releases(tmp_path, home):
    _seed(home, "1.0.2", "1.0.10", current="1.0.10")

    result, _calls = _run(tmp_path, home, latest="1.1.0")

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (_store(home) / "releases").iterdir()) == ["1.0.10", "1.1.0"]


def test_failed_update_still_links_the_existing_release(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home, fail_install=True)

    assert result.returncode != 0
    assert _linked_release(home) == "1.0.3"
    assert [p.name for p in (_store(home) / "releases").iterdir()] == ["1.0.3"]


def test_unreachable_registry_on_a_populated_store_still_links(tmp_path, home):
    _seed(home, "1.0.3", current="1.0.3")

    result, _calls = _run(tmp_path, home, latest="")

    assert result.returncode != 0
    assert _linked_release(home) == "1.0.3"


@pytest.mark.parametrize("latest, fail_install", [("1.1.0", True), ("", False)])
def test_empty_store_fails_loudly(tmp_path, home, latest, fail_install):
    result, _calls = _run(tmp_path, home, latest=latest, fail_install=fail_install)

    assert result.returncode != 0
    assert not (home / ".local/bin/pi").exists()


def test_a_dead_runs_temp_prefix_is_cleared(tmp_path, home):
    _seed(home, "1.1.0", current="1.1.0")
    leftover = _store(home) / "releases/.tmp-abc123"
    leftover.mkdir()

    result, _calls = _run(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert not leftover.exists()
