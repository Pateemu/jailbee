"""Rendering `litellm:` into LiteLLM's config, the callback table and the
per-container file — pure functions, no Incus."""

from jailbee.config.models_litellm import LiteLLMConfig
from jailbee.litellm_render import (
    CATCH_ALL,
    alias,
    container_payload,
    egress_hosts,
    render_callback_data,
    render_instance_config,
    render_instance_env,
)


def _by_name(cfg: dict) -> dict[str, dict]:
    return {m["model_name"]: m for m in cfg["model_list"]}


def test_alias_shape():
    assert alias("default", "sol-xhigh") == "jb-default-sol-xhigh"


def test_instance_config_has_one_deployment_per_route_plus_catch_all():
    rendered = render_instance_config(LiteLLMConfig())
    models = _by_name(rendered)
    assert set(models) == {
        "jb-default-astra",
        "jb-default-sol-xhigh",
        "jb-default-sol-medium",
        "jb-default-luna-high",
        CATCH_ALL,
    }
    sol = models["jb-default-sol-xhigh"]
    assert sol["litellm_params"] == {"model": "chatgpt/gpt-6-sol"}
    assert sol["model_info"] == {"mode": "responses", "max_input_tokens": 1_050_000}


def test_effort_is_not_put_in_litellm_params():
    """Claude Code sends its own effort; the callback applies configured effort."""
    models = _by_name(render_instance_config(LiteLLMConfig()))
    assert "reasoning_effort" not in models["jb-default-sol-xhigh"]["litellm_params"]


def test_route_params_pass_through():
    cfg = LiteLLMConfig.model_validate({"routes": {"astra": {"params": {"timeout": 600}}}})
    models = _by_name(render_instance_config(cfg))
    assert models["jb-default-astra"]["litellm_params"] == {
        "model": "chatgpt/gpt-6-astra",
        "timeout": 600,
    }


def test_catch_all_targets_default_profiles_haiku_route():
    models = _by_name(render_instance_config(LiteLLMConfig()))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6-luna"}


def test_catch_all_falls_back_to_sonnet_when_haiku_unmapped():
    cfg = LiteLLMConfig.model_validate({"profiles": {"codex": {"haiku": None}}})
    models = _by_name(render_instance_config(cfg))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6-sol"}


def test_proxy_settings_always_set():
    rendered = render_instance_config(LiteLLMConfig())
    assert rendered["litellm_settings"] == {
        "drop_params": True,
        "turn_off_message_logging": True,
        "callbacks": ["jailbee_callback.proxy_handler_instance"],
    }
    assert rendered["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}


def test_callback_data_marks_chatgpt_and_efforts():
    data = render_callback_data(LiteLLMConfig())
    assert data["aliases"]["jb-default-sol-xhigh"] == {
        "chatgpt": True,
        "effort": "xhigh",
        "min_effort": None,
    }
    assert data["aliases"]["jb-default-astra"] == {
        "chatgpt": True,
        "effort": None,
        "min_effort": None,
    }
    assert data["catch_all"] == {"chatgpt": True, "effort": "high", "min_effort": None}


def test_callback_data_keeps_effort_floor_for_overridden_route():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"luna-high": {"effort": None, "min_effort": "medium"}}}
    )
    data = render_callback_data(cfg)
    assert data["aliases"]["jb-default-luna-high"] == {
        "chatgpt": True,
        "effort": None,
        "min_effort": "medium",
    }
    assert data["catch_all"] == {"chatgpt": True, "effort": None, "min_effort": "medium"}


def test_instance_env():
    env = render_instance_env(port=4100, master_key="sk-jb-x", account="default")
    assert env.splitlines() == [
        "PORT=4100",
        "LITELLM_MASTER_KEY=sk-jb-x",
        "CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/default/auth",
        "JAILBEE_LITELLM_CALLBACK_DATA=/var/lib/jailbee-litellm/default/callback.json",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
    ]
    assert env.endswith("\n")


def test_container_payload():
    payload = container_payload(
        LiteLLMConfig(),
        base_url="http://10.0.0.3:4100",
        key_file="/etc/jailbee/litellm-default.key",
    )
    assert payload == {
        "version": 1,
        "default_profile": "codex",
        "profiles": {
            "codex": {
                "base_url": "http://10.0.0.3:4100",
                "key_file": "/etc/jailbee/litellm-default.key",
                "effort": None,
                "tiers": {
                    "fable": "jb-default-astra",
                    "opus": "jb-default-sol-xhigh",
                    "sonnet": "jb-default-sol-medium",
                    "haiku": "jb-default-luna-high",
                },
                "context_window": 1_050_000,
            }
        },
    }


def test_payload_context_window_is_the_largest_of_the_profiles_routes():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {"sol-medium": {"context_window": 400_000}},
            "profiles": {"small": {"sonnet": "sol-medium"}},
        }
    )
    payload = container_payload(cfg, base_url="u", key_file="k")
    assert payload["profiles"]["small"]["context_window"] == 400_000
    assert payload["profiles"]["codex"]["context_window"] == 1_050_000


def test_egress_hosts_for_chatgpt():
    assert egress_hosts(LiteLLMConfig()) == ["auth.openai.com", "chatgpt.com"]
