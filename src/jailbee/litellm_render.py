"""`litellm:` config → the files the LiteLLM container and `claude-jb` read.

Pure: no Incus, no filesystem. `litellm` pushes what this returns.

One LiteLLM process per account (`CHATGPT_TOKEN_DIR` is process-wide), so
everything here is rendered per account. An instance serves every API-key
route, plus the subscription routes of the profiles bound to its account.

Effort never goes into a deployment's `litellm_params`: the 2026-09-29 spike
showed a deployment's `reasoning_effort` is only a default, and Claude Code
always sends its own session effort (`output_config.effort`), so the value
would never apply. The jailbee callback (`provision/litellm/jailbee_callback.py`)
applies fixed and floor efforts from `render_callback_data`'s table instead.

Secrets appear in the rendered config only as `os.environ/<NAME>`; their
values live in the per-instance `instance.env`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from jailbee.config.models_litellm import PROVIDER_HOSTS, api_base_endpoint
from jailbee.egress import parse_egress_entry

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig, ResolvedProfile, ResolvedRoute

SCOPE_DEFAULT = "default"
CATCH_ALL = "claude-*"
CONTAINER_STATE_DIR = "/var/lib/jailbee-litellm"
_CHEAPEST_FIRST = ("haiku", "sonnet", "opus", "fable")


def alias(scope: str, route: str) -> str:
    return f"jb-{scope}-{route}"


def container_key_file(account: str) -> str:
    return f"/etc/jailbee/litellm-{account}.key"


def served_routes(cfg: LiteLLMConfig, account: str) -> dict[str, ResolvedRoute]:
    routes = cfg.effective_routes()
    bound = {
        route
        for profile in cfg.effective_profiles().values()
        if cfg.instance_account(profile) == account
        for route in profile.tiers.values()
    }
    return {n: r for n, r in routes.items() if not r.subscription or n in bound}


def _cheapest(
    profile: ResolvedProfile, routes: Mapping[str, ResolvedRoute]
) -> ResolvedRoute | None:
    for tier in _CHEAPEST_FIRST:
        if tier in profile.tiers:
            return routes[profile.tiers[tier]]
    return None


def catch_all_route(cfg: LiteLLMConfig, account: str) -> ResolvedRoute | None:
    """Hard-coded Claude model IDs are background work: the cheapest mapped tier.

    The default profile's, when this instance serves all of it; otherwise the
    first profile (by name) bound to this account; None if no profile is.
    """
    served = served_routes(cfg, account)
    profiles = cfg.effective_profiles()
    default = profiles[cfg.default_profile]
    if all(route in served for route in default.tiers.values()):
        return _cheapest(default, served)
    for name in sorted(profiles):
        if cfg.instance_account(profiles[name]) == account:
            return _cheapest(profiles[name], served)
    return None


def _deployment(model_name: str, route: ResolvedRoute) -> dict[str, object]:
    params: dict[str, object] = {"model": route.model, **route.params}
    if route.api_key:
        params["api_key"] = f"os.environ/{route.api_key}"
    if route.api_base:
        params["api_base"] = route.api_base
    info: dict[str, object] = {"max_input_tokens": route.context_window}
    if route.subscription:
        info = {"mode": "responses", **info}
    return {"model_name": model_name, "litellm_params": params, "model_info": info}


def merge_extra(base: dict[str, object], extra: Mapping[str, object]) -> dict[str, object]:
    """Deep-merge `extra` into a copy of `base`: mappings recurse, lists append, scalars win.

    `litellm_inputs.check_extra` has already refused a fragment whose scalar
    would replace one of jailbee's mappings or lists.
    """
    out = dict(base)
    for key, value in extra.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge_extra(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            out[key] = [*current, *value]
        else:
            out[key] = value
    return out


def render_instance_config(
    cfg: LiteLLMConfig, account: str, *, extra: Mapping[str, object] | None = None
) -> dict[str, object]:
    served = served_routes(cfg, account)
    model_list = [_deployment(alias(SCOPE_DEFAULT, n), r) for n, r in served.items()]
    catch_all = catch_all_route(cfg, account)
    if catch_all is not None:
        model_list.append(_deployment(CATCH_ALL, catch_all))
    rendered: dict[str, object] = {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "turn_off_message_logging": True,
            "callbacks": ["jailbee_callback.proxy_handler_instance"],
        },
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }
    return merge_extra(rendered, extra) if extra else rendered


def _entry(route: ResolvedRoute) -> dict[str, object]:
    # The callback's key stays `chatgpt`: it flattens `system` for that backend only.
    return {"chatgpt": route.subscription, "effort": route.effort, "min_effort": route.min_effort}


def render_callback_data(cfg: LiteLLMConfig, account: str) -> dict[str, object]:
    catch_all = catch_all_route(cfg, account)
    return {
        "aliases": {
            alias(SCOPE_DEFAULT, n): _entry(r) for n, r in served_routes(cfg, account).items()
        },
        "catch_all": None if catch_all is None else _entry(catch_all),
    }


def render_instance_env(
    *, port: int, master_key: str, account: str, secrets: Mapping[str, str] | None = None
) -> str:
    """systemd `EnvironmentFile` that `jailbee litellm login` also sources with bash.

    Single quotes mean the same thing to both parsers only without a quote,
    backslash or newline inside; `litellm_inputs` refuses such values first.
    """
    base = f"{CONTAINER_STATE_DIR}/{account}"
    lines = [
        f"PORT={port}",
        f"LITELLM_MASTER_KEY={master_key}",
        f"CHATGPT_TOKEN_DIR={base}/auth",
        f"JAILBEE_LITELLM_CALLBACK_DATA={base}/callback.json",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
    ]
    for name, value in sorted((secrets or {}).items()):
        if any(c in value for c in "'\\\n\r\0"):
            raise ValueError(f"secret {name} cannot be written to the proxy environment")
        lines.append(f"{name}='{value}'")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class InstanceFiles:
    """What one instance reads from `<CONTAINER_STATE_DIR>/<account>/`."""

    account: str
    config_yaml: str
    callback_json: str
    instance_env: str

    def digest(self, callback_source: str) -> str:
        """Digest of everything the unit reads, the shared callback included."""
        sha = hashlib.sha256()
        for name, text in (
            ("jailbee_callback.py", callback_source),
            ("config.yaml", self.config_yaml),
            ("callback.json", self.callback_json),
            ("instance.env", self.instance_env),
        ):
            sha.update(name.encode() + b"\0" + text.encode() + b"\0")
        return sha.hexdigest()


def render_instance_files(
    cfg: LiteLLMConfig,
    account: str,
    *,
    port: int,
    master_key: str,
    secrets: Mapping[str, str] | None = None,
    extra: Mapping[str, object] | None = None,
) -> InstanceFiles:
    return InstanceFiles(
        account=account,
        config_yaml=yaml.safe_dump(
            render_instance_config(cfg, account, extra=extra), sort_keys=False
        ),
        callback_json=json.dumps(render_callback_data(cfg, account), indent=2) + "\n",
        instance_env=render_instance_env(
            port=port, master_key=master_key, account=account, secrets=secrets
        ),
    )


def container_profiles(
    cfg: LiteLLMConfig, *, base_urls: Mapping[str, str]
) -> dict[str, dict[str, object]]:
    """`claude-jb`'s view of every profile whose account has a running instance."""
    routes = cfg.effective_routes()
    out: dict[str, dict[str, object]] = {}
    for name, profile in cfg.effective_profiles().items():
        account = cfg.instance_account(profile)
        if account not in base_urls:
            continue
        out[name] = {
            "base_url": base_urls[account],
            "key_file": container_key_file(account),
            "effort": profile.effort,
            "tiers": {t: alias(SCOPE_DEFAULT, r) for t, r in profile.tiers.items()},
            "context_window": max(routes[r].context_window for r in profile.tiers.values()),
        }
    return out


def container_payload(cfg: LiteLLMConfig, *, base_urls: Mapping[str, str]) -> dict[str, object]:
    return {
        "version": 1,
        "default_profile": cfg.default_profile,
        "profiles": container_profiles(cfg, base_urls=base_urls),
    }


def _route_egress(route: ResolvedRoute) -> list[str]:
    if route.api_base:
        hosts = [api_base_endpoint(route.api_base)]
    else:
        hosts = [f"{h}:443" for h in PROVIDER_HOSTS.get(route.provider, ())]
    return [*hosts, *route.egress]


def egress_hosts(cfg: LiteLLMConfig) -> list[str]:
    """`host[:port]` entries the proxy container may reach (no port = 443)."""
    served = {n: r for a in cfg.accounts for n, r in served_routes(cfg, a).items()}
    entries = {e for route in served.values() for e in _route_egress(route)}
    entries.update(cfg.egress)
    return sorted(entries)


def upstream_targets(cfg: LiteLLMConfig) -> list[tuple[str, int]]:
    """Hosts `jailbee doctor` probes from inside the proxy; CIDR entries are not probeable."""
    targets: list[tuple[str, int]] = []
    for raw in egress_hosts(cfg):
        spec = parse_egress_entry(raw)
        if spec.is_literal and "/" in spec.target:
            continue
        targets.append((spec.target, spec.port or 443))
    return targets
