"""`litellm:` config → the files the LiteLLM container and `claude-jb` read.

Pure: no Incus, no filesystem. `litellm_state` writes what this returns.

Effort never goes into a deployment's `litellm_params`: the 2026-09-29 spike
showed a deployment's `reasoning_effort` is only a default, and Claude Code
always sends its own session effort (`output_config.effort`), so the value
would never apply. The jailbee callback (`provision/litellm/jailbee_callback.py`)
applies fixed and floor efforts from `render_callback_data`'s table instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig, ResolvedRoute

SCOPE_DEFAULT = "default"
ACCOUNT_DEFAULT = "default"
CATCH_ALL = "claude-*"
CONTAINER_STATE_DIR = "/var/lib/jailbee-litellm"

_PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "chatgpt": ("chatgpt.com", "auth.openai.com"),
}


def alias(scope: str, route: str) -> str:
    return f"jb-{scope}-{route}"


def _deployment(model_name: str, route: ResolvedRoute) -> dict[str, object]:
    return {
        "model_name": model_name,
        "litellm_params": {"model": route.model, **route.params},
        "model_info": {"mode": "responses", "max_input_tokens": route.context_window},
    }


def _catch_all_route(cfg: LiteLLMConfig) -> ResolvedRoute:
    """Hard-coded Claude model IDs are background work: the cheapest mapped tier."""
    profile = cfg.effective_profiles()[cfg.default_profile]
    routes = cfg.effective_routes()
    for tier in ("haiku", "sonnet", "opus", "fable"):
        if tier in profile.tiers:
            return routes[profile.tiers[tier]]
    raise ValueError(f"profile '{profile.name}' maps no tier")


def render_instance_config(cfg: LiteLLMConfig) -> dict[str, object]:
    routes = cfg.effective_routes()
    model_list = [_deployment(alias(SCOPE_DEFAULT, n), r) for n, r in routes.items()]
    model_list.append(_deployment(CATCH_ALL, _catch_all_route(cfg)))
    return {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "turn_off_message_logging": True,
            "callbacks": ["jailbee_callback.proxy_handler_instance"],
        },
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def _entry(route: ResolvedRoute) -> dict[str, object]:
    return {
        "chatgpt": route.model.startswith("chatgpt/"),
        "effort": route.effort,
        "min_effort": route.min_effort,
    }


def render_callback_data(cfg: LiteLLMConfig) -> dict[str, object]:
    routes = cfg.effective_routes()
    return {
        "aliases": {alias(SCOPE_DEFAULT, n): _entry(r) for n, r in routes.items()},
        "catch_all": _entry(_catch_all_route(cfg)),
    }


def render_instance_env(*, port: int, master_key: str, account: str) -> str:
    base = f"{CONTAINER_STATE_DIR}/{account}"
    return "\n".join(
        [
            f"PORT={port}",
            f"LITELLM_MASTER_KEY={master_key}",
            f"CHATGPT_TOKEN_DIR={base}/auth",
            f"JAILBEE_LITELLM_CALLBACK_DATA={base}/callback.json",
            "LITELLM_LOCAL_MODEL_COST_MAP=True",
        ]
    ) + "\n"


def container_payload(cfg: LiteLLMConfig, *, base_url: str, key_file: str) -> dict[str, object]:
    routes = cfg.effective_routes()
    profiles: dict[str, object] = {}
    for name, profile in cfg.effective_profiles().items():
        profiles[name] = {
            "base_url": base_url,
            "key_file": key_file,
            "effort": profile.effort,
            "tiers": {t: alias(SCOPE_DEFAULT, r) for t, r in profile.tiers.items()},
            "context_window": max(routes[r].context_window for r in profile.tiers.values()),
        }
    return {"version": 1, "default_profile": cfg.default_profile, "profiles": profiles}


def egress_hosts(cfg: LiteLLMConfig) -> list[str]:
    providers = {r.model.split("/", 1)[0] for r in cfg.effective_routes().values()}
    return sorted({h for p in providers for h in _PROVIDER_HOSTS[p]})
