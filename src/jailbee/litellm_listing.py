"""`jailbee litellm ls`: what `claude-jb` uses — globally, and in each repo with an override.

Pure: the caller reads the config files and prints the lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.litellm_render import alias

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.config.local_layer import LocalLiteLLMView
    from jailbee.config.models_litellm import LiteLLMConfig, ResolvedRoute


def _effort(route: ResolvedRoute) -> str:
    if route.effort:
        return f"{route.effort} (fixed)"
    if route.min_effort:
        return f">= {route.min_effort}"
    return "session"


def _block(title: str, cfg: LiteLLMConfig, scope: str | None) -> list[str]:
    routes = cfg.effective_routes()
    settings = [
        f"default profile: {cfg.default_profile}",
        f"autostart: {'on' if cfg.autostart else 'off'}",
        f"aliases: {alias(scope, '<route>')}",
    ]
    lines = [title, "  " + " · ".join(settings)]
    for name, profile in sorted(cfg.effective_profiles().items()):
        mark = "*" if name == cfg.default_profile else ""
        session = f" · session effort {profile.effort}" if profile.effort else ""
        lines.append(f"  {name}{mark}  account {cfg.instance_account(profile)}{session}")
        rows = [
            (tier, route, routes[route].model, _effort(routes[route]))
            for tier, route in profile.tiers.items()
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for row in rows:
            cells = "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
            lines.append(f"    {cells}  {routes[row[1]].context_window} tokens")
    return lines


def listing_lines(
    host: LiteLLMConfig, repos: Sequence[LocalLiteLLMView], *, global_origin: str
) -> list[str]:
    """The host's block, then one per repo override, in prefix order."""
    lines = _block(f"global  ({global_origin})", host, None)
    for entry in repos:
        lines += [
            "",
            *_block(
                f"repo {entry.prefix}  ({entry.view.origin})", entry.view.config, entry.view.scope
            ),
        ]
    return lines
