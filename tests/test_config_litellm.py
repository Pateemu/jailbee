"""`litellm:` block — built-ins, field-level merge, validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jailbee.config.errors import ConfigError
from jailbee.config.models_litellm import (
    PINNED_LITELLM_VERSION,
    LiteLLMConfig,
    ResolvedProfile,
    ResolvedRoute,
    api_base_endpoint,
)
from jailbee.global_config import GlobalConfig, validate_global_raw


def test_defaults_are_disabled_with_builtin_codex_profile():
    cfg = LiteLLMConfig()
    assert cfg.enabled is False
    assert cfg.default_profile == "codex"
    assert cfg.effective_profiles()["codex"] == ResolvedProfile(
        name="codex",
        tiers={"fable": "astra", "opus": "sol-xhigh", "sonnet": "sol-medium", "haiku": "luna-high"},
        effort=None,
        account="default",
    )


def test_builtin_routes_carry_spec_models_and_efforts():
    routes = LiteLLMConfig().effective_routes()
    assert routes["astra"] == ResolvedRoute(
        name="astra",
        model="chatgpt/gpt-6-astra",
        effort=None,
        min_effort=None,
        context_window=922_000,
        params={},
    )
    assert (routes["sol-xhigh"].model, routes["sol-xhigh"].effort) == ("chatgpt/gpt-6-sol", "xhigh")
    assert (routes["sol-medium"].model, routes["sol-medium"].effort) == (
        "chatgpt/gpt-6-sol",
        "medium",
    )
    assert (routes["luna-high"].model, routes["luna-high"].effort) == ("chatgpt/gpt-6-luna", "high")


def test_partial_override_of_builtin_route_keeps_its_model():
    cfg = LiteLLMConfig.model_validate({"routes": {"sol-xhigh": {"effort": "max"}}})
    route = cfg.effective_routes()["sol-xhigh"]
    assert route.model == "chatgpt/gpt-6-sol"
    assert route.effort == "max"


def test_override_can_switch_fixed_effort_to_floor():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"luna-high": {"effort": None, "min_effort": "high"}}}
    )
    route = cfg.effective_routes()["luna-high"]
    assert (route.effort, route.min_effort) == (None, "high")


def test_new_route_without_model_is_rejected_by_name():
    with pytest.raises(ValidationError, match="route 'mine' has no model"):
        LiteLLMConfig.model_validate({"routes": {"mine": {"effort": "high"}}})


def test_effort_and_min_effort_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="both `effort` and `min_effort`"):
        LiteLLMConfig.model_validate(
            {"routes": {"x": {"model": "chatgpt/gpt-6-sol", "effort": "high", "min_effort": "low"}}}
        )


def test_unknown_effort_level_is_rejected():
    with pytest.raises(ValidationError):
        LiteLLMConfig.model_validate({"routes": {"sol-xhigh": {"effort": "ultra"}}})


@pytest.mark.parametrize(
    "key",
    [
        "model",
        "custom_llm_provider",
        "api_base",
        "base_url",
        "api_key",
        "extra_headers",
        "API_BASE",
        "Custom_LLM_Provider",
    ],
)
def test_route_params_cannot_redirect_provider_endpoint_or_credential(key):
    with pytest.raises(ValidationError, match=f"route 'astra' params must not set {key}"):
        LiteLLMConfig.model_validate(
            {"routes": {"astra": {"params": {key: "https://evil.example"}}}}
        )


def test_route_params_still_accept_sampling_settings():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"astra": {"params": {"temperature": 0.2, "max_tokens": 4096}}}}
    )
    assert cfg.effective_routes()["astra"].params == {"temperature": 0.2, "max_tokens": 4096}


def test_builtin_gpt6_context_window_is_the_backend_input_limit():
    routes = LiteLLMConfig().effective_routes()
    assert {r.context_window for r in routes.values()} == {922_000}


def test_unknown_chatgpt_model_needs_context_window():
    with pytest.raises(ValidationError, match="route 't' needs `context_window`"):
        LiteLLMConfig.model_validate({"routes": {"t": {"model": "chatgpt/gpt-5.6-terra"}}})
    ok = LiteLLMConfig.model_validate(
        {"routes": {"t": {"model": "chatgpt/gpt-5.6-terra", "context_window": 1_050_000}}}
    )
    assert ok.effective_routes()["t"].context_window == 1_050_000


def test_profile_referencing_missing_route_is_rejected():
    with pytest.raises(ValidationError, match="profile 'p' tier 'opus' names unknown route 'nope'"):
        LiteLLMConfig.model_validate({"profiles": {"p": {"opus": "nope"}}})


def test_partial_profile_override_keeps_other_tiers():
    cfg = LiteLLMConfig.model_validate({"profiles": {"codex": {"sonnet": "luna-high"}}})
    tiers = cfg.effective_profiles()["codex"].tiers
    assert tiers["sonnet"] == "luna-high"
    assert tiers["opus"] == "sol-xhigh"


def test_profile_tier_can_be_unset_with_null():
    cfg = LiteLLMConfig.model_validate({"profiles": {"codex": {"fable": None}}})
    assert "fable" not in cfg.effective_profiles()["codex"].tiers


def test_profile_with_no_tiers_is_rejected():
    with pytest.raises(ValidationError, match="profile 'empty' maps no tier"):
        LiteLLMConfig.model_validate(
            {"profiles": {"empty": {"fable": None, "opus": None, "sonnet": None, "haiku": None}}}
        )


def test_default_profile_must_exist():
    with pytest.raises(ValidationError, match="default_profile 'x' is not a profile"):
        LiteLLMConfig.model_validate({"default_profile": "x"})


def test_version_defaults_to_pin():
    assert LiteLLMConfig().effective_version() == PINNED_LITELLM_VERSION
    assert LiteLLMConfig(version="1.200.0").effective_version() == "1.200.0"


def test_global_config_carries_litellm_block():
    g = GlobalConfig.model_validate({"litellm": {"enabled": True}})
    assert g.litellm.enabled is True


def test_litellm_is_host_level_only():
    from jailbee.config.common import _HOST_LEVEL_KEYS, _split_host_keys

    assert "litellm" in _HOST_LEVEL_KEYS
    host, repo = _split_host_keys({"litellm": {"enabled": True}, "gpg": {"enabled": True}})
    assert host == {"litellm": {"enabled": True}}
    assert repo == {"gpg": {"enabled": True}}


def test_explicit_null_clears_builtin_model_and_is_rejected():
    with pytest.raises(ValidationError, match="route 'astra' has no model"):
        LiteLLMConfig.model_validate({"routes": {"astra": {"model": None}}})


def test_custom_route_and_profile_with_params_and_effort():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {"custom": {"model": "chatgpt/gpt-6-sol", "params": {"temperature": 0.6}}},
            "profiles": {"mine": {"account": "default", "opus": "custom", "effort": "max"}},
            "default_profile": "mine",
        }
    )
    assert cfg.effective_routes()["custom"].params == {"temperature": 0.6}
    assert cfg.effective_profiles()["mine"] == ResolvedProfile(
        name="mine", tiers={"opus": "custom"}, effort="max", account="default"
    )


_KIMI = {
    "model": "openrouter/moonshotai/kimi-k3",
    "context_window": 262144,
    "api_key": "OPENROUTER_API_KEY",
}


def test_accounts_default_to_one_and_codex_is_bound_to_it():
    cfg = LiteLLMConfig()
    assert cfg.accounts == ["default"]
    codex = cfg.effective_profiles()["codex"]
    assert codex.account == "default"
    assert cfg.instance_account(codex) == "default"


@pytest.mark.parametrize("name", ["../x", "Default", "-x", "a" * 33, "x y", ""])
def test_account_names_are_validated(name):
    with pytest.raises(ValidationError, match="invalid account name"):
        LiteLLMConfig.model_validate({"accounts": [name]})


def test_duplicate_accounts_are_rejected():
    with pytest.raises(ValidationError, match="more than once: work"):
        LiteLLMConfig.model_validate({"accounts": ["work", "work"]})


def test_at_least_one_account():
    with pytest.raises(ValidationError):
        LiteLLMConfig.model_validate({"accounts": []})


def test_renaming_the_only_account_asks_to_rebind_codex():
    with pytest.raises(ValidationError, match=r"profiles\.codex\.account"):
        LiteLLMConfig.model_validate({"accounts": ["personal"]})
    cfg = LiteLLMConfig.model_validate(
        {"accounts": ["personal"], "profiles": {"codex": {"account": "personal"}}}
    )
    assert cfg.effective_profiles()["codex"].account == "personal"


def test_subscription_profile_without_account_is_rejected():
    with pytest.raises(ValidationError, match="must name an `account`"):
        LiteLLMConfig.model_validate({"profiles": {"codex": {"account": None}}})


def test_api_key_profile_needs_no_account_and_uses_the_first():
    cfg = LiteLLMConfig.model_validate(
        {
            "accounts": ["default", "work"],
            "routes": {"kimi": _KIMI},
            "profiles": {"kimi": {"opus": "kimi", "sonnet": "kimi", "haiku": "kimi"}},
        }
    )
    kimi = cfg.effective_profiles()["kimi"]
    assert kimi.account is None
    assert cfg.instance_account(kimi) == "default"
    route = cfg.effective_routes()["kimi"]
    assert (route.provider, route.subscription, route.api_key) == (
        "openrouter",
        False,
        "OPENROUTER_API_KEY",
    )


@pytest.mark.parametrize("model", ["mistral/mistral-large", "mistral-large"])
def test_unknown_provider_needs_api_base_or_egress(model):
    route = {"model": model, "context_window": 128000}
    with pytest.raises(ValidationError, match="set `api_base` or `egress`"):
        LiteLLMConfig.model_validate({"routes": {"m": route}})
    LiteLLMConfig.model_validate({"routes": {"m": {**route, "egress": ["api.mistral.ai"]}}})
    LiteLLMConfig.model_validate(
        {"routes": {"m": {**route, "api_base": "https://api.mistral.ai/v1"}}}
    )


@pytest.mark.parametrize(
    "extra", [{"api_key": "OPENAI_API_KEY"}, {"api_base": "https://proxy.example.com"}]
)
def test_subscription_routes_refuse_api_key_and_api_base(extra):
    with pytest.raises(ValidationError, match="jailbee litellm login"):
        LiteLLMConfig.model_validate({"routes": {"astra": extra}})


@pytest.mark.parametrize("name", ["sk-or-v1-abc", "openrouter_key", "1KEY"])
def test_api_key_must_be_a_variable_name(name):
    with pytest.raises(ValidationError, match="NAME of a variable"):
        LiteLLMConfig.model_validate({"routes": {"kimi": {**_KIMI, "api_key": name}}})


@pytest.mark.parametrize(
    "name", ["PORT", "PATH", "LITELLM_MASTER_KEY", "CHATGPT_TOKEN_DIR", "PYTHONPATH", "LD_PRELOAD"]
)
def test_reserved_secret_names_are_refused(name):
    with pytest.raises(ValidationError, match="reserved"):
        LiteLLMConfig.model_validate({"routes": {"kimi": {**_KIMI, "api_key": name}}})


def test_a_pasted_key_is_not_echoed_back():
    raw = {"litellm": {"routes": {"kimi": {**_KIMI, "api_key": "sk-or-v1-deadbeefcafe"}}}}
    with pytest.raises(ConfigError) as caught:
        validate_global_raw(raw, Path("global.yaml"))
    assert "deadbeefcafe" not in str(caught.value)
    assert "litellm.routes.kimi.api_key" in str(caught.value)


def test_other_global_errors_keep_pydantics_text():
    with pytest.raises(ConfigError, match="input_value"):
        validate_global_raw({"update_check": "sometimes"}, Path("global.yaml"))


@pytest.mark.parametrize(
    "url", ["ftp://x.example.com", "not a url", "https://user:pw@x.example.com"]
)
def test_api_base_must_be_a_plain_http_url(url):
    with pytest.raises(ValidationError, match="api_base"):
        LiteLLMConfig.model_validate({"routes": {"kimi": {**_KIMI, "api_base": url}}})


def test_api_base_endpoint_defaults_ports():
    assert api_base_endpoint("https://llm.example.com/v1") == "llm.example.com:443"
    assert api_base_endpoint("http://10.0.0.5:11434") == "10.0.0.5:11434"
    assert api_base_endpoint("http://ollama.lan/v1") == "ollama.lan:80"


@pytest.mark.parametrize("where", ["route", "top"])
def test_egress_entries_are_parsed(where):
    bad = ["api.example.com:99999"]
    raw = {"routes": {"kimi": {**_KIMI, "egress": bad}}} if where == "route" else {"egress": bad}
    with pytest.raises(ValidationError, match="out of range"):
        LiteLLMConfig.model_validate(raw)


def test_extra_is_an_optional_path():
    assert LiteLLMConfig().extra is None
    cfg = LiteLLMConfig.model_validate({"extra": "~/.config/jailbee/litellm/extra.yaml"})
    assert cfg.extra == "~/.config/jailbee/litellm/extra.yaml"
