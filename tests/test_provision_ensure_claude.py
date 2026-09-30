"""Content checks for the ensure-claude.sh provision script.

The script runs inside a container during `gie new`. It must:
- serialize concurrent gie-new runs with flock on the shared dir,
- full-install when the shared version store is empty (unconditionally),
- otherwise repoint the per-container ~/.local/bin/claude symlink,
- run `claude update` only when JAILBEE_CLAUDE_AUTO_UPDATE=true.
These are static-content assertions; behavior is exercised via lifecycle tests.
"""

import importlib.resources


def _script() -> str:
    return importlib.resources.files("jailbee.provision").joinpath("ensure-claude.sh").read_text()


def test_ensure_claude_is_strict_bash():
    script = _script()
    assert script.startswith("#!/bin/bash")
    assert "set -euo pipefail" in script


def test_ensure_claude_locks_shared_dir():
    """flock on a file inside the shared dir serializes parallel gie-new."""
    script = _script()
    assert "flock" in script
    assert ".update.lock" in script


def test_ensure_claude_full_installs_when_versions_empty():
    """Empty version store → run the native installer, regardless of flag."""
    script = _script()
    assert "claude.ai/install.sh" in script


def test_ensure_claude_relinks_bin_symlink():
    """A populated shared store + missing per-container symlink → relink."""
    script = _script()
    assert "ln -sfn" in script
    assert ".local/bin/claude" in script


def test_ensure_claude_update_gated_on_env_flag():
    """`claude update` only runs when JAILBEE_CLAUDE_AUTO_UPDATE=true."""
    script = _script()
    assert "JAILBEE_CLAUDE_AUTO_UPDATE" in script
    assert "claude update" in script


def test_ensure_claude_install_tolerates_installer_nonzero_exit():
    """Defense in depth: the native installer invokes `claude` as a final
    smoke-check, which can exit non-zero for reasons unrelated to the binary
    install (e.g. a not-yet-valid ~/.claude.json). Under `pipefail` that would
    abort the whole script before we relink the per-container symlink. The
    install branch must not let that single failure kill the script — it runs
    the installer tolerantly (`|| ...`) rather than relying solely on pipefail.
    """
    script = _script()
    # The installer pipeline is followed by a `|| <fallback>` so a non-zero
    # exit doesn't abort the script under pipefail. We assert the fallback
    # echo (which only lives on the `||` branch) appears, proving tolerance.
    assert "claude.ai/install.sh | bash" in script
    assert "|| echo" in script
    assert "installer exited non-zero" in script


def test_ensure_claude_verifies_binary_after_install():
    """After the install branch, the script must verify the binary is actually
    present/executable and hard-fail with a clear message if not — instead of
    silently leaving an empty store that later surfaces as an opaque exit-127
    `command not found` in the autostart `claude` step.
    """
    script = _script()
    assert '[ -x "${BIN}" ]' in script or '[ ! -x "${BIN}" ]' in script


def test_ensure_claude_links_only_runnable_semver_release(tmp_path):
    """A half-written or non-executable entry in the shared store must not
    become the launcher target (it dangles once the writer renames/removes it)."""
    import os
    import subprocess

    versions = tmp_path / ".local/share/claude/versions"
    versions.mkdir(parents=True)
    for name, mode in (("2.1.285", 0o755), ("2.1.287.tmp", 0o755), ("2.1.288", 0o644)):
        (versions / name).write_text("#!/bin/sh\nexit 0\n")
        (versions / name).chmod(mode)
    script = importlib.resources.files("jailbee.provision").joinpath("ensure-claude.sh")
    env = {**os.environ, "HOME": str(tmp_path), "JAILBEE_CLAUDE_AUTO_UPDATE": "true"}
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    assert (tmp_path / ".local/bin/claude").resolve() == (versions / "2.1.285").resolve()
