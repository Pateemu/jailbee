"""The callback runs inside the LiteLLM container, not in the host package.

Only the external CustomLogger base is stubbed; transform uses the real code.
"""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

TABLE = {
    "aliases": {
        "jb-default-sol-xhigh": {"chatgpt": True, "effort": "xhigh", "min_effort": None},
        "jb-default-luna-floor": {"chatgpt": True, "effort": None, "min_effort": "high"},
        "jb-default-astra": {"chatgpt": True, "effort": None, "min_effort": None},
    },
    "catch_all": {"chatgpt": True, "effort": "high", "min_effort": None},
}


@pytest.fixture
def cb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    base = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    base.CustomLogger = CustomLogger
    for name in ("litellm", "litellm.integrations"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", base)
    data = tmp_path / "callback.json"
    data.write_text(json.dumps(TABLE))
    monkeypatch.setenv("JAILBEE_LITELLM_CALLBACK_DATA", str(data))
    monkeypatch.delitem(sys.modules, "jailbee.provision.litellm.jailbee_callback", raising=False)
    return importlib.import_module("jailbee.provision.litellm.jailbee_callback")


def _req(model: str, **extra: object) -> dict:
    return {"model": model, "messages": [], **extra}


def test_system_list_is_flattened_for_chatgpt(cb):
    out = cb.transform(
        _req(
            "jb-default-astra",
            system=[
                {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "b"},
            ],
        ),
        TABLE,
    )
    assert out["system"] == "a\n\nb"


def test_non_text_system_blocks_are_not_flattened(cb):
    out = cb.transform(
        _req(
            "jb-default-astra",
            system=[{"type": "text", "text": "a"}, {"type": "image", "source": "other"}],
        ),
        TABLE,
    )
    assert out["system"] == "a"


def test_system_string_untouched(cb):
    assert cb.transform(_req("jb-default-astra", system="s"), TABLE)["system"] == "s"


def test_non_chatgpt_alias_keeps_system_but_applies_effort(cb):
    table = {
        "aliases": {"x": {"chatgpt": False, "effort": "max", "min_effort": None}},
        "catch_all": None,
    }
    req = _req("x", system=[{"type": "text", "text": "a"}], output_config={"effort": "low"})
    out = cb.transform(req, table)
    assert out["system"] == [{"type": "text", "text": "a"}]
    assert out["output_config"] == {"effort": "max"}


def test_fixed_effort_overrides_request(cb):
    out = cb.transform(_req("jb-default-sol-xhigh", output_config={"effort": "low"}), TABLE)
    assert out["output_config"] == {"effort": "xhigh"}


def test_fixed_effort_set_when_request_has_none(cb):
    out = cb.transform(_req("jb-default-sol-xhigh"), TABLE)
    assert out["output_config"] == {"effort": "xhigh"}


def test_floor_raises_low_request(cb):
    out = cb.transform(_req("jb-default-luna-floor", output_config={"effort": "low"}), TABLE)
    assert out["output_config"] == {"effort": "high"}


def test_floor_sets_effort_when_request_has_none(cb):
    out = cb.transform(_req("jb-default-luna-floor"), TABLE)
    assert out["output_config"] == {"effort": "high"}


def test_floor_keeps_higher_request(cb):
    out = cb.transform(_req("jb-default-luna-floor", output_config={"effort": "max"}), TABLE)
    assert out["output_config"] == {"effort": "max"}


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("low", "high"), ("medium", "high"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)
def test_floor_orders_all_supported_efforts(cb, requested, expected):
    out = cb.transform(_req("jb-default-luna-floor", output_config={"effort": requested}), TABLE)
    assert out["output_config"]["effort"] == expected


def test_other_output_config_keys_survive(cb):
    out = cb.transform(
        _req("jb-default-sol-xhigh", output_config={"effort": "low", "format": {"type": "json"}}),
        TABLE,
    )
    assert out["output_config"] == {"effort": "xhigh", "format": {"type": "json"}}


def test_transform_leaves_input_unchanged(cb):
    system = [{"type": "text", "text": "a"}]
    config = {"effort": "low", "format": {"type": "json"}}
    req = _req("jb-default-sol-xhigh", system=system, output_config=config)
    out = cb.transform(req, TABLE)
    assert out["system"] == "a"
    assert out["output_config"]["effort"] == "xhigh"
    assert req["system"] == system
    assert req["output_config"] == config


def test_catch_all_applies_to_claude_ids(cb):
    out = cb.transform(_req("claude-haiku-4-5", system=[{"type": "text", "text": "a"}]), TABLE)
    assert out["system"] == "a"
    assert out["output_config"] == {"effort": "high"}


def test_unknown_model_untouched(cb):
    req = _req("something-else", system=[{"type": "text", "text": "a"}])
    assert cb.transform(dict(req), TABLE) == req


def test_hook_delegates_to_transform(cb):
    data = _req("jb-default-sol-xhigh")
    out = asyncio.run(
        cb.proxy_handler_instance.async_pre_call_hook(None, None, data, "anthropic_messages")
    )
    assert out["output_config"] == {"effort": "xhigh"}
