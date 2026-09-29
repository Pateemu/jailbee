"""Host-side LiteLLM state stays stable, private and independent of Incus."""

import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jailbee import litellm_state as st


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


@pytest.mark.parametrize("name", ["../x", "a/b", "", "Default", "-x", "a" * 33, "x y", ".hidden"])
def test_account_names_cannot_leave_the_state_dir(name: str) -> None:
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.port_for(name)
    with pytest.raises(ValueError, match="invalid LiteLLM account name"):
        st.master_key(name)
    for call in (
        st.check_account,
        st.known_port,
        st.master_key_path,
        lambda n: st.record_applied(n, "x"),
        lambda n: st.config_applied(n, "x"),
    ):
        with pytest.raises(ValueError, match="invalid LiteLLM account name"):
            call(name)
    assert not (st.state_dir().parent / "x").exists()


@pytest.mark.parametrize("name", ["default", "work-2", "a_b", "0"])
def test_ordinary_account_names_are_accepted(name: str) -> None:
    assert st.port_for(name) >= st.BASE_PORT


def test_every_state_directory_is_private_even_when_created_looser() -> None:
    (st.state_dir() / "default").mkdir(parents=True, mode=0o755)
    (st.state_dir() / "default").chmod(0o755)
    st.state_dir().chmod(0o755)
    st.master_key("default")
    for path in (st.state_dir(), st.state_dir() / "default"):
        assert _mode(path) == 0o700, path


def test_write_private_replaces_atomically_and_leaves_no_temp_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    st.record_applied("default", "before")
    target = st.state_dir() / "default" / "applied.sha256"
    before = target.read_text()

    def crash(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(st.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        st.record_applied("default", "after")
    assert target.read_text() == before  # never truncated
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]


def test_corrupt_ports_file_names_the_file() -> None:
    st.state_dir().mkdir(parents=True)
    (st.state_dir() / "ports.json").write_text("{not json")
    with pytest.raises(RuntimeError, match=r"ports\.json is not valid JSON"):
        st.port_for("default")
    (st.state_dir() / "ports.json").write_text("[1, 2]")
    with pytest.raises(RuntimeError, match="must hold a JSON object"):
        st.port_for("default")


def test_known_port_does_not_allocate() -> None:
    assert st.known_port("default") is None
    assert not (st.state_dir() / "ports.json").exists()
    st.port_for("default")
    assert st.known_port("default") == 4100


def test_master_key_path_creates_nothing() -> None:
    path = st.master_key_path("work")
    assert path == st.state_dir() / "work" / "master.key"
    assert not path.parent.exists()


def test_config_applied_compares_the_recorded_digest() -> None:
    assert not st.config_applied("default", "abc")
    st.record_applied("default", "abc")
    assert st.config_applied("default", "abc")
    assert not st.config_applied("default", "def")
    assert (st.state_dir() / "default" / "applied.sha256").stat().st_mode & 0o777 == 0o600
