"""Runs the `claude-jb` script install.sh writes, against a fake `claude`
that prints its environment and argv. Needs bash and jq (both in the golden
image and in the dev environment)."""

import importlib.resources
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_MARKER = "cat > /usr/local/bin/claude-jb <<'EOF'\n"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

PAYLOAD = {
    "version": 1,
    "default_profile": "codex",
    "profiles": {
        "codex": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "KEYFILE",
            "effort": None,
            "tiers": {"opus": "jb-default-sol-xhigh", "haiku": "jb-default-luna-high"},
            "context_window": 1050000,
        },
        "deep": {
            "base_url": "http://10.0.0.3:4100",
            "key_file": "KEYFILE",
            "effort": "max",
            "tiers": {"opus": "jb-default-astra"},
            "context_window": 1050000,
        },
    },
}


@pytest.fixture
def script(tmp_path: Path) -> Path:
    text = importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()
    assert _MARKER in text
    start = text.index(_MARKER) + len(_MARKER)
    body = text[start : text.index("\nEOF\n", start)]
    path = tmp_path / "claude-jb"
    path.write_text(body + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/bash\n"
        "env | grep -E '^(ANTHROPIC|CLAUDE_CODE)_' | sort\n"
        'printf "ARGV:%s\\n" "$@"\n'
    )
    fake.chmod(0o755)
    key = tmp_path / "key"
    key.write_text("sk-jb-secret\n")
    cfg = tmp_path / "litellm.json"
    cfg.write_text(json.dumps(PAYLOAD).replace("KEYFILE", str(key)))
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "JAILBEE_LITELLM_CONFIG": str(cfg),
        # Fake claude prints its environment; no proxy runs in these tests.
        "JAILBEE_LITELLM_SKIP_REACHABILITY": "1",
    }


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script), *args], env=env, capture_output=True, text=True)


def _lines(out: str) -> list[str]:
    return out.splitlines()


def test_default_profile_env(script, env):
    r = _run(script, env, "-p", "hi")
    assert r.returncode == 0, r.stderr
    lines = _lines(r.stdout)
    assert "ANTHROPIC_BASE_URL=http://10.0.0.3:4100" in lines
    assert "ANTHROPIC_AUTH_TOKEN=sk-jb-secret" in lines
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-sol-xhigh" in lines
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL=jb-default-luna-high" in lines
    assert not any(line.startswith("ANTHROPIC_DEFAULT_SONNET_MODEL=") for line in lines)
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS=1050000" in lines
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1" in lines
    assert [line for line in lines if line.startswith("ARGV:")] == ["ARGV:-p", "ARGV:hi"]


def test_profile_flag_is_stripped_and_selects(script, env):
    r = _run(script, env, "--profile", "deep", "-p", "hi")
    lines = _lines(r.stdout)
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in lines
    assert [line for line in lines if line.startswith("ARGV:")] == [
        "ARGV:--effort",
        "ARGV:max",
        "ARGV:-p",
        "ARGV:hi",
    ]


def test_profile_equals_form(script, env):
    r = _run(script, env, "--profile=deep")
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in _lines(r.stdout)


def test_explicit_empty_profile_is_rejected(script, env):
    r = _run(script, env, "--profile=")
    assert r.returncode != 0 and "--profile needs a name" in r.stderr
    assert "ANTHROPIC_AUTH_TOKEN" not in r.stdout


def test_env_var_selects_profile(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_PROFILE": "deep"})
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-astra" in _lines(r.stdout)


def test_flag_beats_env_var(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_PROFILE": "deep"}, "--profile", "codex")
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=jb-default-sol-xhigh" in _lines(r.stdout)


def test_user_effort_wins_over_profile_effort(script, env):
    r = _run(script, env, "--profile", "deep", "--effort", "low")
    argv = [line for line in _lines(r.stdout) if line.startswith("ARGV:")]
    assert argv == ["ARGV:--effort", "ARGV:low"]


def test_user_effort_equals_form_also_wins(script, env):
    r = _run(script, env, "--profile", "deep", "--effort=low")
    argv = [line for line in _lines(r.stdout) if line.startswith("ARGV:")]
    assert argv == ["ARGV:--effort=low"]


def test_unknown_profile_errors(script, env):
    r = _run(script, env, "--profile", "nope")
    assert r.returncode != 0
    assert "unknown profile 'nope'" in r.stderr and "codex" in r.stderr


def test_missing_config_errors_with_fix(script, env, tmp_path):
    r = _run(script, {**env, "JAILBEE_LITELLM_CONFIG": str(tmp_path / "absent.json")})
    assert r.returncode != 0
    assert "jailbee litellm up" in r.stderr and "jailbee apply" in r.stderr


def test_corrupt_config_errors(script, env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    r = _run(script, {**env, "JAILBEE_LITELLM_CONFIG": str(bad)})
    assert r.returncode != 0
    assert "cannot read" in r.stderr


def test_unreadable_key_errors(script, env, tmp_path):
    cfg = json.loads(Path(env["JAILBEE_LITELLM_CONFIG"]).read_text())
    cfg["profiles"]["codex"]["key_file"] = str(tmp_path / "nokey")
    Path(env["JAILBEE_LITELLM_CONFIG"]).write_text(json.dumps(cfg))
    r = _run(script, env)
    assert r.returncode != 0 and "key" in r.stderr


def test_empty_readable_key_errors_before_claude(script, env):
    cfg = json.loads(Path(env["JAILBEE_LITELLM_CONFIG"]).read_text())
    Path(cfg["profiles"]["codex"]["key_file"]).write_text("\n")
    r = _run(script, env)
    assert r.returncode != 0 and "empty" in r.stderr and "jailbee apply" in r.stderr
    assert "ARGV:" not in r.stdout


def test_unreachable_gateway_errors_before_claude(script, env):
    r = _run(script, {**env, "JAILBEE_LITELLM_SKIP_REACHABILITY": ""})
    assert r.returncode != 0 and "jailbee litellm up" in r.stderr
    assert "jailbee apply" in r.stderr and "ARGV:" not in r.stdout


def test_inherited_anthropic_vars_are_replaced(script, env):
    r = _run(script, {**env, "ANTHROPIC_DEFAULT_SONNET_MODEL": "stale", "ANTHROPIC_API_KEY": "x"})
    lines = _lines(r.stdout)
    assert not any(line.startswith("ANTHROPIC_DEFAULT_SONNET_MODEL=") for line in lines)
    assert not any(line.startswith("ANTHROPIC_API_KEY=") for line in lines)
