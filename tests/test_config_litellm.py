"""`litellm:` block — built-ins, field-level merge, validation."""

import pytest
from pydantic import ValidationError

from jailbee.config.models_litellm import (
    PINNED_LITELLM_VERSION,
    LiteLLMConfig,
    ResolvedProfile,
    ResolvedRoute,
)
from jailbee.global_config import GlobalConfig


def test_defaults_are_disabled_with_builtin_codex_profile():
    cfg = LiteLLMConfig()
    assert cfg.enabled is False
    assert cfg.default_profile == "codex"
    assert cfg.effective_profiles()["codex"] == ResolvedProfile(
        name="codex",
        tiers={"fable": "astra", "opus": "sol-xhigh", "sonnet": "sol-medium", "haiku": "luna-high"},
        effort=None,
    )


def test_builtin_routes_carry_spec_models_and_efforts():
    routes = LiteLLMConfig().effective_routes()
    assert routes["astra"] == ResolvedRoute(
        name="astra", model="chatgpt/gpt-6-astra", effort=None, min_effort=None,
        context_window=1_050_000, params={},
    )
    assert (routes["sol-xhigh"].model, routes["sol-xhigh"].effort) == ("chatgpt/gpt-6-sol", "xhigh")
    assert (routes["sol-medium"].model, routes["sol-medium"].effort) == ("chatgpt/gpt-6-sol", "medium")
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


def test_phase1_rejects_non_chatgpt_models():
    with pytest.raises(ValidationError, match="only `chatgpt/` models"):
        LiteLLMConfig.model_validate({"routes": {"k": {"model": "openrouter/moonshotai/kimi-k3"}}})


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
            "profiles": {"mine": {"opus": "custom", "effort": "max"}},
            "default_profile": "mine",
        }
    )
    assert cfg.effective_routes()["custom"].params == {"temperature": 0.6}
    assert cfg.effective_profiles()["mine"] == ResolvedProfile(
        name="mine", tiers={"opus": "custom"}, effort="max"
    )
