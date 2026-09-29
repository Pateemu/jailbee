"""Host-side LiteLLM state stays stable, private and independent of Incus."""

import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path

import pytest
import yaml

from jailbee import litellm_state as st
from jailbee.config.models_litellm import LiteLLMConfig


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_state_dir_under_xdg(xdg: Path) -> None:
    assert st.state_dir() == xdg / "jailbee" / "litellm"


def test_port_allocation_is_stable_and_persisted() -> None:
    assert st.port_for("default") == st.BASE_PORT == 4100
    assert st.port_for("work") == 4101
    assert st.port_for("default") == 4100
    assert json.loads((st.state_dir() / "ports.json").read_text()) == {
        "default": 4100,
        "work": 4101,
    }


def test_master_key_created_once_with_0600() -> None:
    first = st.master_key("default")
    assert first.startswith("sk-jb-") and len(first) > 30
    assert st.master_key("default") == first
    path = st.state_dir() / "default" / "master.key"
    assert path.read_text() == first + "\n"
    assert _mode(path) == 0o600


def test_concurrent_account_allocation_keeps_one_port_and_key(monkeypatch) -> None:
    original = st.secrets.token_urlsafe
    generated: list[str] = []

    def delayed_token(length: int) -> str:
        token = original(length)
        generated.append(token)
        time.sleep(0.01)
        return token

    monkeypatch.setattr(st.secrets, "token_urlsafe", delayed_token)

    def allocate(_: int) -> tuple[int, str]:
        return st.port_for("default"), st.master_key("default")

    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(allocate, range(80)))
    assert len(set(results)) == 1
    assert len(generated) == 1
    assert json.loads((st.state_dir() / "ports.json").read_text()) == {"default": 4100}
    assert _mode(st.state_dir() / "default" / "master.key") == 0o600


def test_write_instance_files_layout_and_modes() -> None:
    result = st.write_instance_files(LiteLLMConfig(), "default")
    base = st.state_dir() / "default"
    assert result.changed is True
    assert yaml.safe_load((base / "config.yaml").read_text())["model_list"]
    assert json.loads((base / "callback.json").read_text())["aliases"]
    env = (base / "instance.env").read_text()
    assert "PORT=4100\n" in env
    assert "LITELLM_MASTER_KEY=sk-jb-" in env
    assert _mode(base / "master.key") == 0o600
    assert _mode(base / "instance.env") == 0o600
    assert _mode(base / "auth") == 0o700
    source = resources.files("jailbee.provision").joinpath("litellm", "jailbee_callback.py")
    assert (st.state_dir() / "callback" / "jailbee_callback.py").read_bytes() == source.read_bytes()


def test_second_write_without_change_reports_unchanged() -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    assert st.write_instance_files(LiteLLMConfig(), "default").changed is False


def test_config_applied_tracks_the_files_the_unit_last_restarted_on() -> None:
    assert st.config_applied("default") is False
    st.write_instance_files(LiteLLMConfig(), "default")
    assert st.config_applied("default") is False  # written, not yet applied
    st.record_applied("default")
    assert st.config_applied("default") is True
    st.write_instance_files(LiteLLMConfig(), "default")
    assert st.config_applied("default") is True  # unchanged rewrite stays applied
    changed = LiteLLMConfig.model_validate({"routes": {"sol-xhigh": {"effort": "max"}}})
    st.write_instance_files(changed, "default")
    assert st.config_applied("default") is False
    st.record_applied("default")
    assert st.config_applied("default") is True


def test_config_change_reports_changed() -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    changed = LiteLLMConfig.model_validate({"routes": {"sol-xhigh": {"effort": "max"}}})
    assert st.write_instance_files(changed, "default").changed is True
    callback = json.loads((st.state_dir() / "default" / "callback.json").read_text())
    assert callback["aliases"]["jb-default-sol-xhigh"]["effort"] == "max"


def test_existing_private_modes_are_restored_without_byte_changes() -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    base = st.state_dir() / "default"
    (base / "auth").chmod(0o755)
    (base / "master.key").chmod(0o644)
    (base / "instance.env").chmod(0o644)
    assert st.write_instance_files(LiteLLMConfig(), "default").changed is False
    assert _mode(base / "auth") == 0o700
    assert _mode(base / "master.key") == 0o600
    assert _mode(base / "instance.env") == 0o600


def test_auth_state_and_logout() -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    auth = st.state_dir() / "default" / "auth" / "auth.json"
    assert st.auth_state("default") == "missing"
    auth.write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))
    assert st.auth_state("default") == "present"
    assert st.logout("default") is True
    assert st.logout("default") is False
    assert st.auth_state("default") == "missing"


@pytest.mark.parametrize(
    "contents",
    ["{not json", "null", "[]", "{}", '{"access_token": "", "refresh_token": ""}'],
)
def test_auth_state_missing_for_invalid_or_empty_auth(contents: str) -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    (st.state_dir() / "default" / "auth" / "auth.json").write_text(contents)
    assert st.auth_state("default") == "missing"


def test_auth_state_present_with_refresh_token_only() -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    (st.state_dir() / "default" / "auth" / "auth.json").write_text(
        json.dumps({"refresh_token": "r"})
    )
    assert st.auth_state("default") == "present"


@pytest.mark.parametrize("name", ["../x", "a/b", "", "Default", "-x", "a" * 33, "x y", ".hidden"])
def test_account_names_cannot_leave_the_state_dir(name: str) -> None:
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.port_for(name)
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.master_key(name)
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.auth_state(name)
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.logout(name)
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.write_instance_files(LiteLLMConfig(), name)
    assert not (st.state_dir().parent / "x").exists()


@pytest.mark.parametrize("name", ["default", "work-2", "a_b", "0"])
def test_ordinary_account_names_are_accepted(name: str) -> None:
    assert st.port_for(name) >= st.BASE_PORT


def test_every_state_directory_is_private_even_when_created_looser() -> None:
    (st.state_dir() / "default").mkdir(parents=True, mode=0o755)
    (st.state_dir() / "default").chmod(0o755)
    st.state_dir().chmod(0o755)
    st.write_instance_files(LiteLLMConfig(), "default")
    for path in (
        st.state_dir(),
        st.state_dir() / "default",
        st.state_dir() / "default" / "auth",
        st.state_dir() / "callback",
    ):
        assert _mode(path) == 0o700, path


def test_write_private_replaces_atomically_and_leaves_no_temp_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    st.write_instance_files(LiteLLMConfig(), "default")
    target = st.state_dir() / "default" / "instance.env"
    before = target.read_text()

    def crash(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(st.os, "replace", crash)
    changed = LiteLLMConfig.model_validate({"routes": {"sol-xhigh": {"effort": "max"}}})
    with pytest.raises(OSError, match="disk full"):
        st.write_instance_files(changed, "default")
    assert target.read_text() == before  # never truncated
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]


def test_corrupt_ports_file_names_the_file() -> None:
    st.state_dir().mkdir(parents=True)
    (st.state_dir() / "ports.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="ports.json is not valid JSON"):
        st.port_for("default")
    (st.state_dir() / "ports.json").write_text("[1, 2]")
    with pytest.raises(RuntimeError, match="must hold a JSON object"):
        st.port_for("default")
