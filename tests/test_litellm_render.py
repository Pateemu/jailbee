"""Rendering `litellm:` into LiteLLM's config, the callback table and the
per-container file — pure functions, no Incus."""

import pytest

from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoOverlay
from jailbee.litellm_render import (
    CATCH_ALL,
    InstanceFiles,
    alias,
    catch_all_route,
    container_key_file,
    container_profiles,
    egress_hosts,
    merge_extra,
    render_callback_data,
    render_instance_config,
    render_instance_env,
    render_instance_files,
    served_routes,
    upstream_targets,
)


def _by_name(cfg: dict) -> dict[str, dict]:
    return {m["model_name"]: m for m in cfg["model_list"]}


def test_alias_shape():
    assert alias(None, "sol-xhigh") == "jb-default-sol-xhigh"
    assert alias("myrepo", "sol-xhigh") == "jb-myrepo.sol-xhigh"


@pytest.mark.parametrize(
    "a,b",
    [
        (("a-b", "c"), ("a", "b-c")),
        (("default", "sol"), (None, "sol")),
        ((None, "a-b"), ("a", "b")),
    ],
)
def test_aliases_of_different_scopes_never_collide(a, b):
    assert alias(*a) != alias(*b)


def test_instance_config_has_one_deployment_per_route_plus_catch_all():
    rendered = render_instance_config(LiteLLMConfig(), "default")
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
    assert sol["model_info"] == {"mode": "responses", "max_input_tokens": 922_000}


def test_effort_is_not_put_in_litellm_params():
    """Claude Code sends its own effort; the callback applies configured effort."""
    models = _by_name(render_instance_config(LiteLLMConfig(), "default"))
    assert "reasoning_effort" not in models["jb-default-sol-xhigh"]["litellm_params"]


def test_route_params_pass_through():
    cfg = LiteLLMConfig.model_validate({"routes": {"astra": {"params": {"timeout": 600}}}})
    models = _by_name(render_instance_config(cfg, "default"))
    assert models["jb-default-astra"]["litellm_params"] == {
        "model": "chatgpt/gpt-6-astra",
        "timeout": 600,
    }


def test_catch_all_targets_default_profiles_haiku_route():
    models = _by_name(render_instance_config(LiteLLMConfig(), "default"))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6-luna"}


def test_catch_all_falls_back_to_sonnet_when_haiku_unmapped():
    cfg = LiteLLMConfig.model_validate({"profiles": {"codex": {"haiku": None}}})
    models = _by_name(render_instance_config(cfg, "default"))
    assert models[CATCH_ALL]["litellm_params"] == {"model": "chatgpt/gpt-6-sol"}


def test_proxy_settings_always_set():
    rendered = render_instance_config(LiteLLMConfig(), "default")
    assert rendered["litellm_settings"] == {
        "drop_params": True,
        "turn_off_message_logging": True,
        "callbacks": ["jailbee_callback.proxy_handler_instance"],
    }
    assert rendered["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}


def test_callback_data_marks_chatgpt_and_efforts():
    data = render_callback_data(LiteLLMConfig(), "default")
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
    data = render_callback_data(cfg, "default")
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


def test_container_profiles():
    profiles = container_profiles(LiteLLMConfig(), base_urls={"default": "http://10.0.0.3:4100"})
    assert profiles == {
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
            "context_window": 922_000,
        }
    }


def test_profile_context_window_is_the_largest_of_the_profiles_routes():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {"sol-medium": {"context_window": 400_000}},
            "profiles": {"small": {"account": "default", "sonnet": "sol-medium"}},
        }
    )
    profiles = container_profiles(cfg, base_urls={"default": "u"})
    assert profiles["small"]["context_window"] == 400_000
    assert profiles["codex"]["context_window"] == 922_000


def test_egress_hosts_for_chatgpt():
    assert egress_hosts(LiteLLMConfig()) == ["auth.openai.com:443", "chatgpt.com:443"]


_KIMI = {
    "model": "openrouter/moonshotai/kimi-k3",
    "context_window": 262144,
    "api_key": "OPENROUTER_API_KEY",
}


def _two_accounts() -> LiteLLMConfig:
    """codex on `personal`; `work` maps Opus to its own low-effort Sol route; kimi everywhere."""
    return LiteLLMConfig.model_validate(
        {
            "accounts": ["personal", "work"],
            "default_profile": "codex",
            "routes": {"kimi": _KIMI, "sol-low": {"model": "chatgpt/gpt-6-sol", "effort": "low"}},
            "profiles": {
                "codex": {"account": "personal"},
                "work": {"account": "work", "opus": "sol-low", "haiku": "kimi"},
                "kimi": {"opus": "kimi", "sonnet": "kimi", "haiku": "kimi"},
            },
        }
    )


def test_subscription_routes_are_served_by_their_accounts_and_api_key_routes_everywhere():
    cfg = _two_accounts()
    assert set(served_routes(cfg, "personal")) == {
        "astra",
        "sol-xhigh",
        "sol-medium",
        "luna-high",
        "kimi",
    }
    assert set(served_routes(cfg, "work")) == {"sol-low", "kimi"}


def test_an_unreferenced_subscription_route_is_served_nowhere():
    cfg = LiteLLMConfig.model_validate({"routes": {"spare": {"model": "chatgpt/gpt-6-luna"}}})
    assert "spare" not in served_routes(cfg, "default")


def test_api_key_is_rendered_as_an_env_reference_never_a_value():
    cfg = LiteLLMConfig.model_validate(
        {"routes": {"kimi": {**_KIMI, "api_base": "https://openrouter.ai/api/v1"}}}
    )
    kimi = _by_name(render_instance_config(cfg, "default"))["jb-default-kimi"]
    assert kimi["litellm_params"] == {
        "model": "openrouter/moonshotai/kimi-k3",
        "api_key": "os.environ/OPENROUTER_API_KEY",
        "api_base": "https://openrouter.ai/api/v1",
    }
    assert kimi["model_info"] == {"max_input_tokens": 262144}  # no Responses mode


def test_catch_all_is_per_account():
    cfg = _two_accounts()
    assert catch_all_route(cfg, "personal").name == "luna-high"  # default profile's haiku
    assert catch_all_route(cfg, "work").name == "kimi"  # first profile bound to work: its haiku


def test_an_api_key_default_profile_is_every_instances_catch_all():
    cfg = _two_accounts().model_copy(update={"default_profile": "kimi"})
    assert catch_all_route(cfg, "personal").name == "kimi"
    assert catch_all_route(cfg, "work").name == "kimi"


def test_an_account_no_profile_uses_has_no_catch_all():
    cfg = LiteLLMConfig.model_validate({"accounts": ["default", "spare"]})
    assert catch_all_route(cfg, "spare") is None
    assert CATCH_ALL not in _by_name(render_instance_config(cfg, "spare"))
    assert render_callback_data(cfg, "spare") == {"aliases": {}, "catch_all": None}


def test_callback_data_covers_only_the_served_aliases():
    data = render_callback_data(_two_accounts(), "work")
    assert set(data["aliases"]) == {"jb-default-sol-low", "jb-default-kimi"}
    assert data["aliases"]["jb-default-kimi"]["chatgpt"] is False
    assert data["aliases"]["jb-default-sol-low"] == {
        "chatgpt": True,
        "effort": "low",
        "min_effort": None,
    }


def test_instance_env_carries_referenced_secrets_single_quoted_and_sorted():
    env = render_instance_env(
        port=4101,
        master_key="sk-jb-x",
        account="work",
        secrets={"XAI_API_KEY": "xai-2", "OPENROUTER_API_KEY": "sk-or-1"},
    )
    lines = env.splitlines()
    assert lines[:2] == ["PORT=4101", "LITELLM_MASTER_KEY=sk-jb-x"]
    assert "CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/work/auth" in lines
    assert lines[-2:] == ["OPENROUTER_API_KEY='sk-or-1'", "XAI_API_KEY='xai-2'"]


def test_instance_env_refuses_a_value_it_cannot_quote():
    with pytest.raises(ValueError):
        render_instance_env(port=1, master_key="k", account="a", secrets={"K": "it's"})


def test_extra_merges_last_with_lists_appended_and_scalars_winning():
    extra = {
        "model_list": [{"model_name": "mine", "litellm_params": {"model": "mistral/large"}}],
        "litellm_settings": {"callbacks": ["my.handler"], "drop_params": False},
        "router_settings": {"num_retries": 2},
    }
    rendered = render_instance_config(LiteLLMConfig(), "default", extra=extra)
    assert [m["model_name"] for m in rendered["model_list"]][-1] == "mine"
    assert rendered["litellm_settings"]["callbacks"] == [
        "jailbee_callback.proxy_handler_instance",
        "my.handler",
    ]
    assert rendered["litellm_settings"]["drop_params"] is False
    assert rendered["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    assert rendered["router_settings"] == {"num_retries": 2}


def test_merge_extra_does_not_mutate_its_inputs():
    base = {"a": {"b": [1]}}
    merge_extra(base, {"a": {"b": [2]}})
    assert base == {"a": {"b": [1]}}


def test_instance_files_digest_changes_with_every_part():
    files = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k1")
    base = files.digest("callback v1")
    assert files.digest("callback v1") == base
    assert files.digest("callback v2") != base
    rekeyed = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k2")
    assert rekeyed.digest("callback v1") != base
    secret = render_instance_files(
        LiteLLMConfig(), "default", port=4100, master_key="k1", secrets={"K": "v"}
    )
    assert secret.digest("callback v1") != base
    assert isinstance(files, InstanceFiles) and files.account == "default"


def test_container_profiles_point_each_profile_at_its_account():
    cfg = _two_accounts()
    profiles = container_profiles(
        cfg, base_urls={"personal": "http://10.0.0.3:4100", "work": "http://10.0.0.3:4101"}
    )
    assert profiles["codex"]["base_url"] == "http://10.0.0.3:4100"
    assert profiles["codex"]["key_file"] == container_key_file("personal")
    assert profiles["work"]["base_url"] == "http://10.0.0.3:4101"
    assert profiles["kimi"]["base_url"] == "http://10.0.0.3:4100"  # accounts[0]
    assert container_key_file("work") == "/etc/jailbee/litellm-work.key"


def test_container_profiles_leave_out_profiles_without_an_instance():
    profiles = container_profiles(_two_accounts(), base_urls={"personal": "http://10.0.0.3:4100"})
    assert set(profiles) == {"codex", "kimi"}


def test_egress_is_derived_from_served_routes_api_base_and_host_egress():
    cfg = LiteLLMConfig.model_validate(
        {
            "routes": {
                "kimi": _KIMI,
                "local": {
                    "model": "openai/qwen",
                    "context_window": 32768,
                    "api_base": "https://llm.example.com:8443/v1",
                    "egress": ["extra.example.com"],
                },
            },
            "egress": ["10.0.0.5:11434"],
        }
    )
    assert egress_hosts(cfg) == sorted(
        [
            "10.0.0.5:11434",
            "auth.openai.com:443",
            "chatgpt.com:443",
            "extra.example.com",
            "llm.example.com:8443",  # api_base replaces api.openai.com
            "openrouter.ai:443",
        ]
    )
    assert ("extra.example.com", 443) in upstream_targets(cfg)
    assert ("llm.example.com", 8443) in upstream_targets(cfg)


def test_upstream_targets_skip_cidrs():
    cfg = LiteLLMConfig.model_validate({"egress": ["10.0.0.0/24"]})
    assert all(host != "10.0.0.0/24" for host, _ in upstream_targets(cfg))


def _scope(**overlay: object) -> LiteLLMConfig:
    return LiteLLMConfig().with_overlay(LiteLLMRepoOverlay.model_validate(overlay))


def test_every_scope_is_rendered_beside_the_host_routes():
    scopes = {"myrepo": _scope(routes={"sol-xhigh": {"effort": "max"}})}
    models = _by_name(render_instance_config(LiteLLMConfig(), "default", scopes=scopes))
    assert "jb-default-sol-xhigh" in models and "jb-myrepo.sol-xhigh" in models
    assert "jb-myrepo.astra" in models
    table = render_callback_data(LiteLLMConfig(), "default", scopes=scopes)["aliases"]
    assert table["jb-myrepo.sol-xhigh"]["effort"] == "max"
    assert table["jb-default-sol-xhigh"]["effort"] == "xhigh"


def test_the_catch_all_stays_the_hosts():
    scopes = {"myrepo": _scope(routes={"luna-high": {"model": "chatgpt/gpt-6-sol"}})}
    models = _by_name(render_instance_config(LiteLLMConfig(), "default", scopes=scopes))
    assert models[CATCH_ALL]["litellm_params"]["model"] == "chatgpt/gpt-6-luna"


def test_a_scope_serves_its_subscription_routes_only_on_its_profiles_account():
    host = LiteLLMConfig.model_validate({"accounts": ["default", "work"]})
    scopes = {
        "myrepo": host.with_overlay(
            LiteLLMRepoOverlay.model_validate({"profiles": {"codex": {"account": "work"}}})
        )
    }
    on_work = _by_name(render_instance_config(host, "work", scopes=scopes))
    on_default = _by_name(render_instance_config(host, "default", scopes=scopes))
    assert "jb-myrepo.sol-xhigh" in on_work and "jb-myrepo.sol-xhigh" not in on_default
    assert "jb-default-sol-xhigh" in on_default and "jb-default-sol-xhigh" not in on_work


def test_container_profiles_use_the_scope_aliases():
    cfg = _scope(routes={"sol-xhigh": {"effort": "max"}})
    profiles = container_profiles(cfg, base_urls={"default": "u"}, scope="myrepo")
    assert profiles["codex"]["tiers"]["opus"] == "jb-myrepo.sol-xhigh"
    host = container_profiles(LiteLLMConfig(), base_urls={"default": "u"})
    assert host["codex"]["tiers"]["opus"] == "jb-default-sol-xhigh"


def test_egress_and_probe_targets_include_every_scope():
    scopes = {
        "myrepo": _scope(
            routes={
                "kimi": {
                    "model": "openrouter/moonshotai/kimi-k3",
                    "context_window": 262144,
                    "api_key": "OPENROUTER_API_KEY",
                }
            }
        )
    }
    assert "openrouter.ai:443" in egress_hosts(LiteLLMConfig(), scopes=scopes)
    assert "openrouter.ai:443" not in egress_hosts(LiteLLMConfig())
    assert ("openrouter.ai", 443) in upstream_targets(LiteLLMConfig(), scopes=scopes)


def test_instance_files_digest_changes_when_a_scope_is_added():
    base = render_instance_files(LiteLLMConfig(), "default", port=4100, master_key="k")
    scoped = render_instance_files(
        LiteLLMConfig(),
        "default",
        port=4100,
        master_key="k",
        scopes={"myrepo": _scope(routes={"sol-xhigh": {"effort": "max"}})},
    )
    assert base.digest("cb") != scoped.digest("cb")


def _api_route(model: str, effort: str) -> dict[str, object]:
    return {"model": model, "context_window": 200000, "effort": effort, "api_key": "K"}


def test_scopes_whose_prefix_and_route_names_interleave_stay_apart_on_one_instance():
    scopes = {
        "a-b": _scope(routes={"c": _api_route("openrouter/m-ab-c", "low")}),
        "a": _scope(routes={"b-c": _api_route("openrouter/m-a-bc", "high")}),
        "default": _scope(
            routes={
                "sol-xhigh": {
                    "model": "chatgpt/gpt-6-other",
                    "context_window": 200000,
                    "effort": "max",
                }
            }
        ),
    }
    host = LiteLLMConfig()
    rendered = render_instance_config(host, "default", scopes=scopes)
    names = [m["model_name"] for m in rendered["model_list"]]
    assert len(names) == len(set(names))
    models = _by_name(rendered)
    assert models["jb-a-b.c"]["litellm_params"]["model"] == "openrouter/m-ab-c"
    assert models["jb-a.b-c"]["litellm_params"]["model"] == "openrouter/m-a-bc"
    assert models["jb-default.sol-xhigh"]["litellm_params"]["model"] == "chatgpt/gpt-6-other"
    assert models["jb-default-sol-xhigh"]["litellm_params"]["model"] == "chatgpt/gpt-6-sol"

    table = render_callback_data(host, "default", scopes=scopes)["aliases"]
    for name in ("jb-a-b.c", "jb-a.b-c", "jb-default.sol-xhigh", "jb-default-sol-xhigh"):
        assert name in table
    assert table["jb-a-b.c"]["effort"] == "low"
    assert table["jb-a.b-c"]["effort"] == "high"
    assert table["jb-default.sol-xhigh"]["effort"] == "max"
    assert table["jb-default-sol-xhigh"]["effort"] == "xhigh"
    assert set(table) == set(names) - {CATCH_ALL}
