"""The callback runs inside the LiteLLM container, not in the host package.

Only the external CustomLogger base is stubbed; transform uses the real code.
"""

import asyncio
import hashlib
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


HOT = {"callback": TABLE, "models": []}


class _Info:
    def __init__(self, id_):
        self.id = id_


class StubDeployment:
    """Stands in for litellm.types.router.Deployment: needs litellm_params, like the real one."""

    def __init__(self, **kw):
        if "litellm_params" not in kw:
            raise ValueError("litellm_params: field required")
        self.model_info = _Info((kw.get("model_info") or {}).get("id"))


class FakeRouter:
    def __init__(self, ids=()):
        self.model_list = [{"model_info": {"id": i}} for i in ids]
        self.calls: list[tuple[str, str]] = []

    def upsert_deployment(self, deployment):
        self.calls.append(("upsert", deployment.model_info.id))

    def delete_deployment(self, dep_id):
        self.calls.append(("delete", dep_id))


def dep(dep_id: str) -> dict:
    return {
        "model_name": dep_id,
        "litellm_params": {"model": "openai/x"},
        "model_info": {"id": dep_id},
    }


@pytest.fixture
def cb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    base = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    base.CustomLogger = CustomLogger
    for name in ("litellm", "litellm.integrations", "litellm.types"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", base)
    router_types = types.ModuleType("litellm.types.router")
    router_types.Deployment = StubDeployment
    monkeypatch.setitem(sys.modules, "litellm.types.router", router_types)
    (tmp_path / "hot.json").write_text(json.dumps(HOT))
    monkeypatch.setenv("JAILBEE_LITELLM_HOT_FILE", str(tmp_path / "hot.json"))
    monkeypatch.setenv("JAILBEE_LITELLM_ACK_FILE", str(tmp_path / "applied.json"))
    monkeypatch.delitem(sys.modules, "jailbee.provision.litellm.jailbee_callback", raising=False)
    return importlib.import_module("jailbee.provision.litellm.jailbee_callback")


def _write_hot(tmp_path: Path, callback: dict, models: list) -> str:
    """Write hot.json the way the host does (replace, so the inode changes); return its digest."""
    text = json.dumps({"callback": callback, "models": models})
    tmp = tmp_path / "hot.json.new"
    tmp.write_text(text)
    tmp.replace(tmp_path / "hot.json")
    return hashlib.sha256(text.encode()).hexdigest()


def _ack(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "applied.json").read_text())


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


def test_non_text_system_blocks_are_dropped_when_flattening(cb):
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


def test_hook_ignores_other_call_types(cb):
    data = _req("jb-default-sol-xhigh", system=[{"type": "text", "text": "a"}])
    for call_type in ("acompletion", "aresponses", "embeddings", None):
        out = asyncio.run(
            cb.proxy_handler_instance.async_pre_call_hook(None, None, dict(data), call_type)
        )
        assert out == data, call_type


def test_hook_accepts_the_enum_shaped_call_type(cb):
    class CallType:  # LiteLLM's CallTypes is an Enum whose `.value` is the wire name
        value = "anthropic_messages"

    out = asyncio.run(
        cb.proxy_handler_instance.async_pre_call_hook(
            None, None, _req("jb-default-sol-xhigh"), CallType()
        )
    )
    assert out["output_config"] == {"effort": "xhigh"}


def test_missing_hot_file_variable_stops_the_proxy_at_start(cb, monkeypatch):
    monkeypatch.delenv("JAILBEE_LITELLM_HOT_FILE")
    with pytest.raises(RuntimeError, match="JAILBEE_LITELLM_HOT_FILE is not set"):
        cb.JailbeeCallback()


def test_first_reload_with_a_router_acknowledges_the_start_state(cb, tmp_path):
    digest = _write_hot(tmp_path, TABLE, [dep("jb:a")])
    handler, router = cb.JailbeeCallback(), FakeRouter(["jb:a"])
    handler.reload_once(router)
    assert router.calls == [("upsert", "jb:a")]
    assert _ack(tmp_path) == {"hot_digest": digest, "error": None}


def test_a_changed_file_upserts_first_then_deletes_what_it_no_longer_lists(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter(["jb:old"])
    handler.reload_once(router)
    router.calls.clear()
    digest = _write_hot(tmp_path, TABLE, [dep("jb:new")])
    handler.reload_once(router)
    assert router.calls == [("upsert", "jb:new"), ("delete", "jb:old")]
    assert _ack(tmp_path)["hot_digest"] == digest


def test_an_unchanged_file_is_not_reconciled_again(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter()
    handler.reload_once(router)
    router.calls.clear()
    handler.reload_once(router)
    assert router.calls == []


def test_the_alias_table_swaps_with_the_models(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter()
    handler.reload_once(router)
    new_table = {
        "aliases": {"jb-x": {"chatgpt": False, "effort": "low", "min_effort": None}},
        "catch_all": None,
    }
    _write_hot(tmp_path, new_table, [])
    handler.reload_once(router)
    assert handler._table == new_table


def test_without_a_router_nothing_is_acknowledged_until_one_exists(cb, tmp_path):
    handler = cb.JailbeeCallback()
    handler.reload_once(None)
    assert not (tmp_path / "applied.json").exists()
    new_table = {"aliases": {}, "catch_all": None}
    digest = _write_hot(tmp_path, new_table, [dep("jb:a")])
    handler.reload_once(None)
    assert handler._table == TABLE  # the old table keeps serving
    handler.reload_once(FakeRouter())
    assert _ack(tmp_path) == {"hot_digest": digest, "error": None}
    assert handler._table == new_table


def test_an_unreadable_file_keeps_the_old_state_and_reports_it(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter(["jb:a"])
    handler.reload_once(router)
    router.calls.clear()
    bad = tmp_path / "hot.json.new"
    bad.write_text("{not json")
    bad.replace(tmp_path / "hot.json")
    handler.reload_once(router)
    assert router.calls == [] and handler._table == TABLE
    ack = _ack(tmp_path)
    assert ack["error"].startswith("cannot read")
    assert ack["hot_digest"] == hashlib.sha256(b"{not json").hexdigest()


def test_a_deployment_without_an_id_changes_nothing(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter(["jb:a"])
    handler.reload_once(router)
    router.calls.clear()
    _write_hot(tmp_path, TABLE, [{"model_name": "x", "litellm_params": {"model": "openai/x"}}])
    handler.reload_once(router)
    assert router.calls == []  # not even the delete of jb:a
    assert "model_info.id" in _ack(tmp_path)["error"]


def test_one_invalid_deployment_among_good_ones_changes_nothing(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter()
    handler.reload_once(router)
    router.calls.clear()
    broken = {"model_name": "b", "model_info": {"id": "jb:b"}}  # no litellm_params
    _write_hot(tmp_path, TABLE, [dep("jb:a"), broken])
    handler.reload_once(router)
    assert router.calls == []
    assert "cannot apply" in _ack(tmp_path)["error"]


def test_a_good_file_after_a_bad_one_clears_the_error(cb, tmp_path):
    handler, router = cb.JailbeeCallback(), FakeRouter()
    handler.reload_once(router)
    _write_hot(tmp_path, TABLE, [{"model_name": "x"}])
    handler.reload_once(router)
    assert _ack(tmp_path)["error"] is not None
    digest = _write_hot(tmp_path, TABLE, [dep("jb:a")])
    handler.reload_once(router)
    assert _ack(tmp_path) == {"hot_digest": digest, "error": None}


def test_the_watcher_starts_in_a_running_loop_and_reloads(cb, tmp_path, monkeypatch):
    router = FakeRouter()
    monkeypatch.setattr(cb, "_router", lambda: router)
    monkeypatch.setattr(cb, "_POLL_SECONDS", 0.01)

    async def scenario():
        await cb.proxy_handler_instance.async_pre_call_hook(None, None, _req("x"), "completion")
        digest = _write_hot(tmp_path, TABLE, [dep("jb:a")])
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (tmp_path / "applied.json").exists() and _ack(tmp_path)["hot_digest"] == digest:
                break
        return digest

    digest = asyncio.run(scenario())
    assert ("upsert", "jb:a") in router.calls and _ack(tmp_path)["hot_digest"] == digest
