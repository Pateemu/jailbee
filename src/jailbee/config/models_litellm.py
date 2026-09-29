"""`litellm:` — Claude Code on non-Anthropic models through a LiteLLM proxy.

Host-level only (`common._HOST_LEVEL_KEYS`): routes name this host's proxy,
subscription logins and secrets file, which a teammate does not have.

A **route** is one model with its settings. A **profile** maps Claude Code's
four tiers to routes and names the **account** whose proxy instance serves it.
An account is one subscription login and one LiteLLM process: LiteLLM reads
`CHATGPT_TOKEN_DIR` once per process (spike, spec §3). Jailbee ships the
`codex` profile, its routes and the `default` account; a user entry with the
same name overlays the built-in one field by field (`model_fields_set` decides
what was written), so `routes: {sol-xhigh: {effort: max}}` keeps the built-in
model.

Validation never reads `secrets.env` or the `extra` fragment: those are host
files `jailbee litellm up` reads (`litellm_inputs`), so a missing secret breaks
only `up`, not every command that loads `global.yaml`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jailbee.egress import parse_egress_entry

PINNED_LITELLM_VERSION = "1.103.0"
"""The version `provision/litellm/requirements.lock` was compiled for."""

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
TIERS: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

DEFAULT_ACCOUNT = "default"
ACCOUNT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
"""An account name becomes a state path component, a systemd instance name and
a dev-container key file name, so it must not be able to leave any of them."""

SUBSCRIPTION_PROVIDERS: frozenset[str] = frozenset({"chatgpt"})
"""Providers that log in with `jailbee litellm login` instead of taking an API key."""

PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "chatgpt": ("chatgpt.com", "auth.openai.com"),
    "openai": ("api.openai.com",),
    "openrouter": ("openrouter.ai",),
    "xai": ("api.x.ai",),
    "gemini": ("generativelanguage.googleapis.com",),
    "deepseek": ("api.deepseek.com",),
}
"""Hosts (port 443) the proxy must reach, per LiteLLM provider prefix. A provider
missing here needs `api_base` or `egress` on its route: never a silent allow."""

KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "chatgpt/gpt-6-astra": 922_000,
    "chatgpt/gpt-6-sol": 922_000,
    "chatgpt/gpt-6-luna": 922_000,
}
"""Window Claude Code manages per model. This is the subscription backend's
maximum *input*, not the API's 1.05M total: the 2026-09-29 spike saw 903k
accepted and denser prompts over ~922k rejected. Claude Code compacts a fixed
reserve below this value, so a larger one would compact after the backend has
already refused the prompt."""

PARAMS_DENYLIST: frozenset[str] = frozenset(
    {
        "model",
        "custom_llm_provider",
        "api_base",
        "base_url",
        "api_key",
        "api_version",
        "organization",
        "headers",
        "extra_headers",
        "litellm_credential_name",
        "azure_ad_token",
        "model_info",
    }
)
"""`params` keys that change which provider, endpoint or credential a deployment
uses. `api_key` and `api_base` have their own validated route fields; raw
params would bypass the egress table derived from them and could send the
login token to another host."""

_SECRET_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_RESERVED_SECRET_NAMES = frozenset({"PORT", "PATH", "HOME"})
_RESERVED_SECRET_PREFIXES = ("LITELLM_", "JAILBEE_", "CHATGPT_", "PYTHON", "LD_")

_BUILTIN_ROUTES: dict[str, dict[str, object]] = {
    "astra": {"model": "chatgpt/gpt-6-astra"},
    "sol-xhigh": {"model": "chatgpt/gpt-6-sol", "effort": "xhigh"},
    "sol-medium": {"model": "chatgpt/gpt-6-sol", "effort": "medium"},
    "luna-high": {"model": "chatgpt/gpt-6-luna", "effort": "high"},
}
_BUILTIN_PROFILES: dict[str, dict[str, object]] = {
    "codex": {
        "account": DEFAULT_ACCOUNT,
        "fable": "astra",
        "opus": "sol-xhigh",
        "sonnet": "sol-medium",
        "haiku": "luna-high",
    },
}


def provider_of(model: str) -> str:
    """LiteLLM's provider prefix (`openrouter/moonshotai/kimi-k3` → `openrouter`); "" if none."""
    return model.split("/", 1)[0] if "/" in model else ""


def api_base_endpoint(url: str) -> str:
    """The `host:port` the proxy must reach for `api_base`.

    Messages never echo the URL: it may carry a token in its query string.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("api_base must be an http:// or https:// URL with a host")
    if parts.username or parts.password:
        raise ValueError(
            "api_base must not carry credentials; put the key in secrets.env and name it in api_key"
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname}:{port}"


def check_secret_name(name: str) -> str:
    """A secrets.env variable name that cannot shadow the proxy's own environment.

    The first message never echoes `name`: someone who got it wrong may have
    pasted the key itself.
    """
    if not _SECRET_NAME.fullmatch(name):
        raise ValueError(
            "api_key takes the NAME of a variable in secrets.env (A-Z, 0-9, _), "
            "never the key itself"
        )
    if name in _RESERVED_SECRET_NAMES or name.startswith(_RESERVED_SECRET_PREFIXES):
        raise ValueError(f"secret name {name!r} is reserved for the proxy's own environment")
    return name


def _check_egress(entries: list[str]) -> list[str]:
    for entry in entries:
        parse_egress_entry(entry)
    return entries


class LiteLLMRoute(BaseModel):
    """One model and its settings; every field is optional for built-in overlays."""

    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM model string, e.g. `chatgpt/gpt-6-sol` or "
            "`openrouter/moonshotai/kimi-k3`. Required for a route jailbee does not ship."
        ),
    )
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Fixed reasoning effort: set on every request to this route, so Claude "
            "Code's `/effort` has no effect on it. Lets two tiers share one model "
            "and still differ. Mutually exclusive with `min_effort`."
        ),
    )
    min_effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Effort floor: a lower requested effort is raised to this; `/effort` "
            "works above it. Mutually exclusive with `effort`."
        ),
    )
    context_window: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Context window in tokens that Claude Code manages (use the backend's "
            "maximum input). Passed to Claude Code as `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. "
            "Defaults to 922000 for `chatgpt/gpt-6-*`; required for any other model."
        ),
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "Name of the variable in `~/.config/jailbee/litellm/secrets.env` that holds "
            "this route's API key: the name, never the key. Not allowed on `chatgpt/` "
            "routes, which log in with `jailbee litellm login`."
        ),
    )
    api_base: str | None = Field(
        default=None,
        description=(
            "Provider endpoint URL passed to LiteLLM. Its host replaces the provider's "
            "default hosts in the proxy's egress allowlist. Not allowed on `chatgpt/` routes."
        ),
    )
    egress: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `host[:port]` entries (port 443 by default) the proxy may reach for "
            "this route. Needed for a provider jailbee has no host table for, unless "
            "`api_base` is set."
        ),
    )
    params: dict[str, object] = Field(
        default_factory=dict,
        description="Raw `litellm_params` merged into this route's deployment.",
    )

    @field_validator("api_key")
    @classmethod
    def _api_key_is_a_name(cls, value: str | None) -> str | None:
        return None if value is None else check_secret_name(value)

    @field_validator("api_base")
    @classmethod
    def _api_base_is_a_url(cls, value: str | None) -> str | None:
        if value is not None:
            api_base_endpoint(value)
        return value

    @field_validator("egress")
    @classmethod
    def _egress_parses(cls, value: list[str]) -> list[str]:
        return _check_egress(value)


class LiteLLMProfile(BaseModel):
    """Claude Code tier → route. An explicit `null` unmaps a built-in tier."""

    model_config = ConfigDict(extra="forbid")
    account: str | None = Field(
        default=None,
        description=(
            "Account (from `litellm.accounts`) whose proxy instance serves this profile. "
            "Required when the profile maps a `chatgpt/` route; a profile of API-key "
            "routes only is served by the first account's instance when unset."
        ),
    )
    fable: str | None = Field(default=None, description="Route for Claude Code's Fable tier.")
    opus: str | None = Field(default=None, description="Route for Claude Code's Opus tier.")
    sonnet: str | None = Field(default=None, description="Route for Claude Code's Sonnet tier.")
    haiku: str | None = Field(default=None, description="Route for Claude Code's Haiku tier.")
    effort: EffortLevel | None = Field(
        default=None,
        description=(
            "Session default effort: `claude-jb` passes `--effort <value>` unless "
            "you pass `--effort` yourself."
        ),
    )


@dataclass(frozen=True)
class ResolvedRoute:
    name: str
    model: str
    effort: str | None
    min_effort: str | None
    context_window: int
    params: dict[str, object]
    api_key: str | None = None
    api_base: str | None = None
    egress: tuple[str, ...] = ()

    @property
    def provider(self) -> str:
        return provider_of(self.model)

    @property
    def subscription(self) -> bool:
        return self.provider in SUBSCRIPTION_PROVIDERS


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    tiers: dict[str, str]
    effort: str | None
    account: str | None = None


def _overlay(builtin: dict[str, object], user: BaseModel | None) -> dict[str, object]:
    merged = dict(builtin)
    if user is not None:
        for key in user.model_fields_set:
            merged[key] = getattr(user, key)
    return merged


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class LiteLLMConfig(BaseModel):
    """Host-level `litellm:` block."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Turns on the jailbee-managed LiteLLM proxy (`jailbee litellm up`) and "
            "`claude-jb` in containers. Off by default."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "LiteLLM version to install. Defaults to the version jailbee pins and "
            "hash-locks; any other value installs without the hash lock and warns."
        ),
    )
    accounts: list[str] = Field(
        default_factory=lambda: [DEFAULT_ACCOUNT],
        min_length=1,
        description=(
            "Subscription logins, one LiteLLM instance each (`jailbee litellm login "
            "<account>`). The built-in `codex` profile uses `default`: set "
            "`profiles.codex.account` when you rename or drop it."
        ),
    )
    default_profile: str = Field(
        default="codex",
        description="Profile `claude-jb` uses when neither `--profile` nor "
        "`JAILBEE_LITELLM_PROFILE` names one.",
    )
    routes: dict[str, LiteLLMRoute] = Field(
        default_factory=dict,
        description=(
            "Named models. An entry named like a built-in route (`astra`, "
            "`sol-xhigh`, `sol-medium`, `luna-high`) overrides it field by field."
        ),
    )
    profiles: dict[str, LiteLLMProfile] = Field(
        default_factory=dict,
        description=(
            "Named tier maps. An entry named like a built-in profile (`codex`) "
            "overrides it tier by tier; `null` unmaps a tier."
        ),
    )
    egress: list[str] = Field(
        default_factory=list,
        description=(
            "Extra `host[:port]` entries (port 443 by default) the proxy may reach, for "
            "deployments added through `extra`. Routes' hosts are derived automatically."
        ),
    )
    extra: str | None = Field(
        default=None,
        description=(
            "Path to a raw LiteLLM config fragment deep-merged into every instance's "
            "config last (lists appended). It may not define `jb-*` or `claude-*` models "
            "or `general_settings.master_key`."
        ),
    )

    @field_validator("accounts")
    @classmethod
    def _account_names(cls, value: list[str]) -> list[str]:
        for account in value:
            if not ACCOUNT_NAME_RE.fullmatch(account):
                raise ValueError(
                    f"invalid account name {account!r}: use 1-32 lowercase letters, digits, "
                    "'-' or '_', starting with a letter or digit"
                )
        repeated = sorted({a for a in value if value.count(a) > 1})
        if repeated:
            raise ValueError(f"accounts listed more than once: {', '.join(repeated)}")
        return value

    @field_validator("egress")
    @classmethod
    def _egress_parses(cls, value: list[str]) -> list[str]:
        return _check_egress(value)

    def effective_version(self) -> str:
        return self.version or PINNED_LITELLM_VERSION

    def instance_account(self, profile: ResolvedProfile) -> str:
        """The account whose instance serves `profile`; API-key-only profiles use the first."""
        return profile.account or self.accounts[0]

    def effective_routes(self) -> dict[str, ResolvedRoute]:
        out: dict[str, ResolvedRoute] = {}
        for name in [*_BUILTIN_ROUTES, *(n for n in self.routes if n not in _BUILTIN_ROUTES)]:
            raw = _overlay(_BUILTIN_ROUTES.get(name, {}), self.routes.get(name))
            model = raw.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError(f"route '{name}' has no model")
            provider = provider_of(model)
            api_key = _optional_str(raw.get("api_key"))
            api_base = _optional_str(raw.get("api_base"))
            egress = raw.get("egress") or []
            assert isinstance(egress, list)
            if provider in SUBSCRIPTION_PROVIDERS and (api_key or api_base):
                raise ValueError(
                    f"route '{name}': `{provider}/` routes log in with `jailbee litellm "
                    "login`; `api_key` and `api_base` are not allowed on them"
                )
            if provider not in PROVIDER_HOSTS and not api_base and not egress:
                what = f"provider {provider!r}" if provider else f"model {model!r}"
                raise ValueError(
                    f"route '{name}': jailbee has no egress hosts for {what}; "
                    "set `api_base` or `egress`"
                )
            effort, min_effort = raw.get("effort"), raw.get("min_effort")
            if effort is not None and min_effort is not None:
                raise ValueError(f"route '{name}' sets both `effort` and `min_effort`")
            window = raw.get("context_window") or KNOWN_CONTEXT_WINDOWS.get(model)
            if not isinstance(window, int):
                raise ValueError(f"route '{name}' needs `context_window` (unknown model {model!r})")
            params = raw.get("params") or {}
            assert isinstance(params, dict)
            forbidden = sorted(k for k in params if str(k).lower() in PARAMS_DENYLIST)
            if forbidden:
                raise ValueError(
                    f"route '{name}' params must not set {', '.join(forbidden)}: "
                    "they change the provider, endpoint or credential"
                )
            out[name] = ResolvedRoute(
                name=name,
                model=model,
                effort=_optional_str(effort),
                min_effort=_optional_str(min_effort),
                context_window=window,
                params=dict(params),
                api_key=api_key,
                api_base=api_base,
                egress=tuple(str(e) for e in egress),
            )
        return out

    def effective_profiles(self) -> dict[str, ResolvedProfile]:
        out: dict[str, ResolvedProfile] = {}
        for name in [*_BUILTIN_PROFILES, *(n for n in self.profiles if n not in _BUILTIN_PROFILES)]:
            raw = _overlay(_BUILTIN_PROFILES.get(name, {}), self.profiles.get(name))
            tiers = {t: str(raw[t]) for t in TIERS if raw.get(t) is not None}
            out[name] = ResolvedProfile(
                name=name,
                tiers=tiers,
                effort=_optional_str(raw.get("effort")),
                account=_optional_str(raw.get("account")),
            )
        return out

    @model_validator(mode="after")
    def _check(self) -> LiteLLMConfig:
        routes = self.effective_routes()
        profiles = self.effective_profiles()
        for profile in profiles.values():
            if not profile.tiers:
                raise ValueError(f"profile '{profile.name}' maps no tier")
            for tier, route in profile.tiers.items():
                if route not in routes:
                    raise ValueError(
                        f"profile '{profile.name}' tier '{tier}' names unknown route '{route}'"
                    )
            if profile.account is not None and profile.account not in self.accounts:
                raise ValueError(
                    f"profile '{profile.name}' uses account '{profile.account}', which is not "
                    f"in `litellm.accounts` ({', '.join(self.accounts)}): add it there or set "
                    f"`profiles.{profile.name}.account`"
                )
            if profile.account is None and any(
                routes[r].subscription for r in profile.tiers.values()
            ):
                raise ValueError(
                    f"profile '{profile.name}' maps a subscription (`chatgpt/`) route, so it "
                    "must name an `account`"
                )
        if self.default_profile not in profiles:
            raise ValueError(f"default_profile '{self.default_profile}' is not a profile")
        return self
