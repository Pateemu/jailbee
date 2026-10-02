"""Host files `jailbee litellm up` reads: secrets.env and the `extra` fragment."""

from pathlib import Path

import pytest

from jailbee.config.models_litellm import LiteLLMConfig
from jailbee.litellm_inputs import (
    LiteLLMInputError,
    load_extra,
    load_host_inputs,
    load_secrets,
    referenced_secrets,
    secrets_path,
)

_KIMI = {
    "model": "openrouter/moonshotai/kimi-k3",
    "context_window": 262144,
    "api_key": "OPENROUTER_API_KEY",
}


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _kimi_cfg(**extra: object) -> LiteLLMConfig:
    return LiteLLMConfig.model_validate({"routes": {"kimi": _KIMI}, **extra})


def _write_secrets(text: str, mode: int = 0o600) -> Path:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_secrets_path_is_under_the_config_home(config_home: Path):
    assert secrets_path() == config_home / "jailbee" / "litellm" / "secrets.env"


def test_no_api_key_route_reads_nothing():
    assert load_secrets(LiteLLMConfig(), None) == {}  # no file needed


def test_missing_file_names_the_path_and_the_names():
    with pytest.raises(LiteLLMInputError, match=r"OPENROUTER_API_KEY.*secrets\.env"):
        load_secrets(_kimi_cfg(), None)


def test_group_or_world_readable_file_is_refused():
    _write_secrets("OPENROUTER_API_KEY=sk-or-1\n", mode=0o640)
    with pytest.raises(LiteLLMInputError, match="chmod 600"):
        load_secrets(_kimi_cfg(), None)


@pytest.mark.parametrize("text", ["OTHER=x\n", "OPENROUTER_API_KEY=\n"])
def test_missing_or_empty_name_is_refused(text: str):
    _write_secrets(text)
    with pytest.raises(LiteLLMInputError, match="does not define OPENROUTER_API_KEY"):
        load_secrets(_kimi_cfg(), None)


def test_only_referenced_names_are_returned_with_quotes_and_export_stripped():
    _write_secrets("# keys\n\nexport OPENROUTER_API_KEY='sk-or-1'\nUNUSED=\"sk-unused\"\n")
    assert load_secrets(_kimi_cfg(), None) == {"OPENROUTER_API_KEY": "sk-or-1"}


@pytest.mark.parametrize("line", ["OPENROUTER_API_KEY=sk'or", "OPENROUTER_API_KEY=sk\\or"])
def test_a_value_the_env_file_cannot_carry_is_refused_without_echo(line: str):
    _write_secrets(line + "\n")
    with pytest.raises(LiteLLMInputError, match="OPENROUTER_API_KEY") as caught:
        load_secrets(_kimi_cfg(), None)
    assert line.split("=", 1)[1] not in str(caught.value)


def test_a_malformed_line_is_refused_without_echo():
    _write_secrets("sk-or-v1-pasted-without-a-name\n")
    with pytest.raises(LiteLLMInputError, match=r"secrets\.env:1") as caught:
        load_secrets(_kimi_cfg(), None)
    assert "pasted" not in str(caught.value)


_NON_UTF8 = b"OPENROUTER_API_KEY=sk-or-\xff\xfe-tail\n"


def _assert_no_file_byte(message: str) -> None:
    # A codec error would print "can't decode byte 0xff in position 25".
    for fragment in ("sk-or", "tail", "0xff", "\\xff", "\xff", "position"):
        assert fragment not in message


def _deny_reading(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """A root-owned file, without depending on the suite not running as root."""
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def _assert_unchained(error: BaseException) -> None:
    assert error.__cause__ is None and error.__suppress_context__


def test_an_unreadable_secrets_file_is_an_input_error(monkeypatch: pytest.MonkeyPatch):
    path = _write_secrets("OPENROUTER_API_KEY=sk-or-1\n")
    _deny_reading(monkeypatch, path)
    with pytest.raises(
        LiteLLMInputError, match=r"cannot read .*secrets\.env: Permission denied"
    ) as caught:
        load_secrets(_kimi_cfg(), None)
    _assert_no_file_byte(str(caught.value))
    _assert_unchained(caught.value)


def test_a_non_utf8_secrets_file_is_an_input_error_without_its_bytes():
    path = _write_secrets("")
    path.write_bytes(_NON_UTF8)
    with pytest.raises(LiteLLMInputError, match=r"secrets\.env: it is not UTF-8 text") as caught:
        load_secrets(_kimi_cfg(), None)
    _assert_no_file_byte(str(caught.value))
    _assert_unchained(caught.value)


def _write_extra(tmp_path: Path, text: str) -> str:
    path = tmp_path / "extra.yaml"
    path.write_text(text)
    return str(path)


def test_no_extra_is_none_and_an_empty_file_is_empty(tmp_path: Path):
    assert load_extra(LiteLLMConfig()) is None
    cfg = LiteLLMConfig.model_validate({"extra": _write_extra(tmp_path, "")})
    assert load_extra(cfg) == {}


def test_extra_secret_references_are_exported(tmp_path: Path):
    fragment = (
        "model_list:\n"
        "  - model_name: mine\n"
        "    litellm_params: {model: mistral/large, api_key: os.environ/MISTRAL_API_KEY}\n"
    )
    cfg = _kimi_cfg(extra=_write_extra(tmp_path, fragment))
    assert referenced_secrets(cfg, load_extra(cfg)) == ["MISTRAL_API_KEY", "OPENROUTER_API_KEY"]


def test_extra_may_not_reference_a_reserved_name(tmp_path: Path):
    fragment = "router_settings: {redis_password: os.environ/LITELLM_MASTER_KEY}\n"
    cfg = LiteLLMConfig.model_validate({"extra": _write_extra(tmp_path, fragment)})
    with pytest.raises(LiteLLMInputError, match="reserved"):
        load_extra(cfg)


def test_invalid_extra_yaml_reports_the_line_not_the_content(tmp_path: Path):
    cfg = LiteLLMConfig.model_validate(
        {"extra": _write_extra(tmp_path, "a: 1\nb: [sk-secret-xyz: :\n")}
    )
    with pytest.raises(LiteLLMInputError, match="not valid YAML") as caught:
        load_extra(cfg)
    assert "sk-secret-xyz" not in str(caught.value)


def test_missing_extra_file_is_an_error(tmp_path: Path):
    cfg = LiteLLMConfig.model_validate({"extra": str(tmp_path / "absent.yaml")})
    with pytest.raises(LiteLLMInputError, match=r"cannot read litellm\.extra"):
        load_extra(cfg)


def test_an_unreadable_extra_file_is_an_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = Path(_write_extra(tmp_path, "router_settings: {redis_password: sk-or-1}\n"))
    _deny_reading(monkeypatch, path)
    cfg = LiteLLMConfig.model_validate({"extra": str(path)})
    with pytest.raises(
        LiteLLMInputError, match=r"cannot read litellm\.extra .*: Permission denied"
    ) as caught:
        load_extra(cfg)
    _assert_no_file_byte(str(caught.value))
    _assert_unchained(caught.value)


def test_a_non_utf8_extra_file_is_an_input_error_without_its_bytes(tmp_path: Path):
    path = tmp_path / "extra.yaml"
    path.write_bytes(_NON_UTF8)
    cfg = LiteLLMConfig.model_validate({"extra": str(path)})
    with pytest.raises(LiteLLMInputError, match=r"extra\.yaml: it is not UTF-8 text") as caught:
        load_extra(cfg)
    _assert_no_file_byte(str(caught.value))
    _assert_unchained(caught.value)


@pytest.mark.parametrize(
    "fragment, message",
    [
        ("[1, 2]\n", "must be a mapping"),
        ("model_list:\n  - model_name: jb-default-astra\n", "jailbee's"),
        ("model_list:\n  - model_name: jb.codex.capable\n", "jailbee's"),
        ("model_list:\n  - model_name: claude-*\n", "jailbee's"),
        ("general_settings: {master_key: sk-mine}\n", "master_key"),
        ("general_settings: open\n", "general_settings must be a mapping"),
        ("litellm_settings: []\n", "litellm_settings must be a mapping"),
        ("router_settings: fast\n", "router_settings must be a mapping"),
        ("environment_variables: [X]\n", "environment_variables must be a mapping"),
        ("environment_variables: {CHATGPT_TOKEN_DIR: /tmp}\n", "reserved"),
        ("model_list: {}\n", "model_list must be a list"),
        ("litellm_settings: {callbacks: mine.handler}\n", "callbacks must be a list"),
    ],
)
def test_extra_cannot_replace_or_redefine_what_jailbee_owns(
    tmp_path: Path, fragment: str, message: str
):
    cfg = LiteLLMConfig.model_validate({"extra": _write_extra(tmp_path, fragment)})
    with pytest.raises(LiteLLMInputError, match=message):
        load_extra(cfg)


def test_load_host_inputs_bundles_both(tmp_path: Path):
    _write_secrets("OPENROUTER_API_KEY=sk-or-1\n")
    cfg = _kimi_cfg(extra=_write_extra(tmp_path, "router_settings: {num_retries: 2}\n"))
    inputs = load_host_inputs(cfg)
    assert inputs.secrets == {"OPENROUTER_API_KEY": "sk-or-1"}
    assert inputs.extra == {"router_settings": {"num_retries": 2}}


def test_secrets_referenced_only_by_a_repo_scope_are_loaded():
    from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoOverlay
    from jailbee.litellm_inputs import referenced_secrets

    scope = LiteLLMConfig().with_overlay(
        LiteLLMRepoOverlay.model_validate(
            {
                "routes": {
                    "kimi": {
                        "model": "openrouter/moonshotai/kimi-k3",
                        "context_window": 262144,
                        "api_key": "OPENROUTER_API_KEY",
                    }
                }
            }
        )
    )
    assert referenced_secrets(LiteLLMConfig(), None) == []
    assert referenced_secrets(LiteLLMConfig(), None, [scope]) == ["OPENROUTER_API_KEY"]


def _repo_scope_with_key(name: str):
    from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoOverlay

    return LiteLLMConfig().with_overlay(
        LiteLLMRepoOverlay.model_validate(
            {
                "routes": {
                    "kimi": {
                        "model": "openrouter/moonshotai/kimi-k3",
                        "context_window": 262144,
                        "api_key": name,
                    }
                }
            }
        )
    )


def test_load_host_inputs_loads_a_secret_only_a_repo_scope_references(config_home: Path):
    from jailbee.config.models_litellm import LiteLLMConfig

    _write_secrets("OPENROUTER_API_KEY=sk-repo\nUNUSED=x\n")
    scope = _repo_scope_with_key("OPENROUTER_API_KEY")
    assert load_host_inputs(LiteLLMConfig()).secrets == {}
    assert load_host_inputs(LiteLLMConfig(), [scope]).secrets == {"OPENROUTER_API_KEY": "sk-repo"}


def test_a_secret_only_a_repo_scope_references_is_reported_when_missing(config_home: Path):
    from jailbee.config.models_litellm import LiteLLMConfig

    _write_secrets("OTHER=x\n")
    scope = _repo_scope_with_key("OPENROUTER_API_KEY")
    with pytest.raises(LiteLLMInputError, match="does not define OPENROUTER_API_KEY"):
        load_host_inputs(LiteLLMConfig(), [scope])
    with pytest.raises(LiteLLMInputError, match="does not define OPENROUTER_API_KEY"):
        load_secrets(LiteLLMConfig(), None, scopes=[scope])


def test_a_missing_secret_names_the_repo_file_that_references_it(config_home: Path):
    from jailbee.config.models_litellm import LiteLLMConfig

    _write_secrets("OTHER=x\nSTORED=sk-live-abc123\n")
    scope = _repo_scope_with_key("OPENROUTER_API_KEY")
    with pytest.raises(LiteLLMInputError, match=r"named by /r/app\.yaml") as caught:
        load_host_inputs(LiteLLMConfig(), [scope], ["/r/app.yaml"])
    assert "sk-live-abc123" not in str(caught.value)
    with pytest.raises(LiteLLMInputError, match=r"named by /r/app\.yaml"):
        load_secrets(LiteLLMConfig(), None, scopes=[scope], scope_labels=["/r/app.yaml"])


def test_a_file_free_of_the_missing_secret_is_not_blamed(config_home: Path):
    from jailbee.config.models_litellm import LiteLLMConfig

    _write_secrets("OTHER=x\n")
    scopes = [_repo_scope_with_key("OPENROUTER_API_KEY"), LiteLLMConfig()]
    with pytest.raises(LiteLLMInputError) as caught:
        load_host_inputs(LiteLLMConfig(), scopes, ["/r/a.yaml", "/r/b.yaml"])
    assert "/r/a.yaml" in str(caught.value) and "/r/b.yaml" not in str(caught.value)


def _overlay_scope(host: LiteLLMConfig, routes: dict[str, object]) -> LiteLLMConfig:
    from jailbee.config.models_litellm import LiteLLMRepoOverlay

    return host.with_overlay(LiteLLMRepoOverlay.model_validate({"routes": routes}))


def test_a_secret_named_only_by_the_host_is_not_blamed_on_a_repo_file(config_home: Path):
    """A scope is host + overlay, so the host's own route shows up in every scope."""
    _write_secrets("OTHER=x\n")
    host = _kimi_cfg()
    scope = _overlay_scope(host, {"sol-high": {"effort": "max"}})
    with pytest.raises(LiteLLMInputError, match="does not define OPENROUTER_API_KEY") as caught:
        load_host_inputs(host, [scope], ["/r/app.yaml"])
    assert "/r/app.yaml" not in str(caught.value)
    assert "named by" not in str(caught.value)


def test_an_override_that_repoints_a_host_route_key_is_blamed(config_home: Path):
    _write_secrets("OPENROUTER_API_KEY=sk-or-1\n")
    host = _kimi_cfg()
    scope = _overlay_scope(host, {"kimi": {"api_key": "OTHER_KEY"}})
    with pytest.raises(
        LiteLLMInputError, match=r"does not define OTHER_KEY.*named by /r/app\.yaml"
    ):
        load_host_inputs(host, [scope], ["/r/app.yaml"])


def test_an_override_that_adds_a_keyed_route_is_blamed_but_an_untouching_one_is_not(
    config_home: Path,
):
    _write_secrets("OTHER=x\n")
    host = LiteLLMConfig()
    adds = _overlay_scope(
        host, {"mine": {"model": "openrouter/x/y", "context_window": 1000, "api_key": "MINE_KEY"}}
    )
    with pytest.raises(LiteLLMInputError) as caught:
        load_host_inputs(host, [adds, _overlay_scope(host, {})], ["/r/a.yaml", "/r/b.yaml"])
    assert "MINE_KEY" in str(caught.value)
    assert "/r/a.yaml" in str(caught.value) and "/r/b.yaml" not in str(caught.value)
