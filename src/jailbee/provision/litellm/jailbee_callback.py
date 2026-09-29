"""Jailbee's LiteLLM pre-call hook, installed inside the proxy container.

On ``/v1/messages``, chatgpt/ deployments need Claude Code's list-valued
system blocks as a string: LiteLLM sends a list as system *messages*, which
the ChatGPT backend rejects. A string becomes Responses instructions.
The callback also replaces or floors Claude Code's requested effort because
its ``output_config.effort`` overrides deployment defaults in LiteLLM.

Only ``/v1/messages`` requests are rewritten (LiteLLM's ``anthropic_messages``
call type); anything else passes through untouched.

The per-instance alias table is read once from ``$JAILBEE_LITELLM_CALLBACK_DATA``.
It is required: without it no request would be flattened and every one would fail
upstream, so a missing variable stops the proxy at start instead.
This module needs only the standard library and LiteLLM's CustomLogger base.
"""

from __future__ import annotations

import json
import os
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_ORDER = ("low", "medium", "high", "xhigh", "max")
_MESSAGES_CALL_TYPE = "anthropic_messages"


def _load_table() -> dict[str, Any]:
    path = os.environ.get("JAILBEE_LITELLM_CALLBACK_DATA")
    if not path:
        raise RuntimeError(
            "JAILBEE_LITELLM_CALLBACK_DATA is not set: the jailbee callback has no alias "
            "table, so it would forward every request unflattened. Run `jailbee litellm up`."
        )
    with open(path) as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


def _entry(model: object, table: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(model, str):
        return None
    found = table.get("aliases", {}).get(model)
    if found is not None:
        return dict(found)
    if model.startswith("claude-") and table.get("catch_all"):
        return dict(table["catch_all"])
    return None


def transform(data: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    """Apply the alias entry without mutating the original request or table."""
    entry = _entry(data.get("model"), table)
    if entry is None:
        return data
    out = dict(data)
    system = out.get("system")
    if entry.get("chatgpt") and isinstance(system, list):
        out["system"] = "\n\n".join(
            str(block.get("text", ""))
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
    fixed, floor = entry.get("effort"), entry.get("min_effort")
    if fixed or floor:
        config = dict(data.get("output_config") or {})
        current = config.get("effort")
        if fixed:
            config["effort"] = fixed
        elif current not in _ORDER or _ORDER.index(current) < _ORDER.index(floor):
            config["effort"] = floor
        out["output_config"] = config
    return out


class JailbeeCallback(CustomLogger):  # type: ignore[misc]  # LiteLLM's base is untyped
    def __init__(self) -> None:
        super().__init__()
        self._table = _load_table()

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: Any
    ) -> dict[str, Any]:
        if str(getattr(call_type, "value", call_type)) != _MESSAGES_CALL_TYPE:
            return data
        return transform(data, self._table)


proxy_handler_instance = JailbeeCallback()
