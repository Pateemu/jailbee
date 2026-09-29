"""`litellm:` — Claude Code on non-Anthropic models through a LiteLLM proxy.

Host-level only (`common._HOST_LEVEL_KEYS`): routes name this host's proxy
and subscription login, which a teammate does not have.

Three concepts. A **route** is one model with its settings. A **profile**
maps Claude Code's four tiers to routes. Jailbee ships the `codex` profile and
its routes; a user entry with the same name overlays the built-in one field by
field (`model_fields_set` decides what was written), so
`routes: {sol-xhigh: {effort: max}}` keeps the built-in model.

Phase 1 of the design (`.local/superpowers/specs/2026-09-29-litellm-gateway-design.md`)
accepts `chatgpt/` models only, served by one implicit account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PINNED_LITELLM_VERSION = "1.103.0"
"""The version `provision/litellm/requirements.lock` was compiled for."""

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
TIERS: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "chatgpt/gpt-6-astra": 1_050_000,
    "chatgpt/gpt-6-sol": 1_050_000,
    "chatgpt/gpt-6-luna": 1_050_000,
}
"""Total window (input + output) per model, from developers.openai.com and the
2026-09-29 spike (the subscription backend accepted 903k input tokens)."""

_BUILTIN_ROUTES: dict[str, dict[str, object]] = {
    "astra": {"model": "chatgpt/gpt-6-astra"},
    "sol-xhigh": {"model": "chatgpt/gpt-6-sol", "effort": "xhigh"},
    "sol-medium": {"model": "chatgpt/gpt-6-sol", "effort": "medium"},
    "luna-high": {"model": "chatgpt/gpt-6-luna", "effort": "high"},
}
_BUILTIN_PROFILES: dict[str, dict[str, object]] = {
    "codex": {"fable": "astra", "opus": "sol-xhigh", "sonnet": "sol-medium", "haiku": "luna-high"},
}


class LiteLLMRoute(BaseModel):
    """One model and its settings; every field is optional for built-in overlays."""

    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(
        default=None,
        description=(
            "LiteLLM model string, e.g. `chatgpt/gpt-6-sol`. Required for a route "
            "jailbee does not ship. This release accepts `chatgpt/` models only."
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
            "Total context window in tokens (input + output). Passed to Claude Code "
            "as `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. Defaults to 1050000 for "
            "`chatgpt/gpt-6-*`; required for any other model."
        ),
    )
    params: dict[str, object] = Field(
        default_factory=dict,
        description="Raw `litellm_params` merged into this route's deployment.",
    )


class LiteLLMProfile(BaseModel):
    """Claude Code tier → route. An explicit `null` unmaps a built-in tier."""

    model_config = ConfigDict(extra="forbid")
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


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    tiers: dict[str, str]
    effort: str | None


def _overlay(builtin: dict[str, object], user: BaseModel | None) -> dict[str, object]:
    merged = dict(builtin)
    if user is not None:
        for key in user.model_fields_set:
            merged[key] = getattr(user, key)
    return merged


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

    def effective_version(self) -> str:
        return self.version or PINNED_LITELLM_VERSION

    def effective_routes(self) -> dict[str, ResolvedRoute]:
        out: dict[str, ResolvedRoute] = {}
        for name in [*_BUILTIN_ROUTES, *(n for n in self.routes if n not in _BUILTIN_ROUTES)]:
            raw = _overlay(_BUILTIN_ROUTES.get(name, {}), self.routes.get(name))
            model = raw.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError(f"route '{name}' has no model")
            if not model.startswith("chatgpt/"):
                raise ValueError(
                    f"route '{name}': only `chatgpt/` models are supported in this "
                    f"release (got {model!r})"
                )
            effort, min_effort = raw.get("effort"), raw.get("min_effort")
            if effort is not None and min_effort is not None:
                raise ValueError(f"route '{name}' sets both `effort` and `min_effort`")
            window = raw.get("context_window") or KNOWN_CONTEXT_WINDOWS.get(model)
            if not isinstance(window, int):
                raise ValueError(f"route '{name}' needs `context_window` (unknown model {model!r})")
            params = raw.get("params") or {}
            assert isinstance(params, dict)
            out[name] = ResolvedRoute(
                name=name,
                model=model,
                effort=effort if isinstance(effort, str) else None,
                min_effort=min_effort if isinstance(min_effort, str) else None,
                context_window=window,
                params=dict(params),
            )
        return out

    def effective_profiles(self) -> dict[str, ResolvedProfile]:
        out: dict[str, ResolvedProfile] = {}
        for name in [*_BUILTIN_PROFILES, *(n for n in self.profiles if n not in _BUILTIN_PROFILES)]:
            raw = _overlay(_BUILTIN_PROFILES.get(name, {}), self.profiles.get(name))
            tiers = {t: str(raw[t]) for t in TIERS if raw.get(t) is not None}
            effort = raw.get("effort")
            out[name] = ResolvedProfile(
                name=name, tiers=tiers, effort=effort if isinstance(effort, str) else None
            )
        return out

    @model_validator(mode="after")
    def _check(self) -> LiteLLMConfig:
        routes = self.effective_routes()
        profiles = self.effective_profiles()
        for profile in profiles.values():
            for tier, route in profile.tiers.items():
                if route not in routes:
                    raise ValueError(
                        f"profile '{profile.name}' tier '{tier}' names unknown route '{route}'"
                    )
        if self.default_profile not in profiles:
            raise ValueError(f"default_profile '{self.default_profile}' is not a profile")
        return self
