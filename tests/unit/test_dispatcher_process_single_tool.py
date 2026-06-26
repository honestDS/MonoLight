import json
from types import SimpleNamespace

import pytest

from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.models.message import MessageRole


def _patch_process_single_tool_side_effects(monkeypatch):
    monkeypatch.setattr(process_single_tool_module.LogManager, "log_tool_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(process_single_tool_module.LogManager, "log_tool_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        process_single_tool_module,
        "truncate_tool_messages_for_budget",
        lambda *args, **kwargs: SimpleNamespace(truncated_count=0),
    )


@pytest.mark.asyncio
async def test_process_single_tool_returns_error_when_tool_disabled(monkeypatch):
    _patch_process_single_tool_side_effects(monkeypatch)

    async def fail_audit(*args, **kwargs):
        raise AssertionError("audit should not run for disabled tools")

    monkeypatch.setattr(process_single_tool_module, "audit_tool_call", fail_audit)

    tool_call = SimpleNamespace(id="call_1", name="execute_shell", arguments={"cmd": "pwd"})
    cfg = SimpleNamespace(tool=SimpleNamespace(enabled_tools=[]))

    tool_msg = await process_single_tool(
        tool_call=tool_call,
        db=None,
        profile=SimpleNamespace(id=1),
        cfg=cfg,
        messages=[],
        username="tester",
        session_id="session_1",
        turn=1,
        uid="user_1",
    )
    payload = json.loads(tool_msg.content)

    assert tool_msg.role == MessageRole.TOOL
    assert tool_msg.tool_call_id == "call_1"
    assert payload == {
        "error": "Tool execute_shell is not enabled in the active profile",
        "tool_name": "execute_shell",
        "status": "failed",
    }


@pytest.mark.asyncio
async def test_process_single_tool_applies_enabled_tools_before_knowledge_base_whitelist(monkeypatch):
    _patch_process_single_tool_side_effects(monkeypatch)

    async def allow_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(process_single_tool_module, "audit_tool_call", allow_audit)

    tool_call = SimpleNamespace(id="call_2", name="query_knowledge_base", arguments={"knowledge_base_id": 123, "query": "hello"})
    cfg = SimpleNamespace(tool=SimpleNamespace(enabled_tools=["query_knowledge_base"]))

    tool_msg = await process_single_tool(
        tool_call=tool_call,
        db=None,
        profile=SimpleNamespace(id=1),
        cfg=cfg,
        messages=[],
        username="tester",
        session_id="session_1",
        turn=1,
        uid="user_1",
        allowed_knowledge_base_ids=[],
    )
    payload = json.loads(tool_msg.content)

    assert tool_msg.role == MessageRole.TOOL
    assert tool_msg.tool_call_id == "call_2"
    assert payload == {
        "error": "Unauthorized knowledge_base_id: 123. It is not in the whitelist of allowed knowledge bases.",
    }


@pytest.mark.asyncio
async def test_process_single_tool_queues_background_task_and_strips_control_arg(monkeypatch):
    _patch_process_single_tool_side_effects(monkeypatch)

    async def allow_audit(*args, **kwargs):
        return None

    submitted = {}

    class FakeBackgroundTaskManager:
        async def submit(self, db, **kwargs):
            submitted["db"] = db
            submitted.update(kwargs)
            return SimpleNamespace(id=42)

    monkeypatch.setattr(process_single_tool_module, "audit_tool_call", allow_audit)
    monkeypatch.setitem(process_single_tool_module.TOOL_EXECUTOR_MAP, "firecrawl_search", object)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.background_tasks.manager",
        SimpleNamespace(background_task_manager=FakeBackgroundTaskManager()),
    )

    tool_call = SimpleNamespace(id="call_bg", name="firecrawl_search", arguments={"query": "mono", "run_in_background": True})
    cfg = SimpleNamespace(tool=SimpleNamespace(enabled_tools=["firecrawl_search"]))

    tool_msg = await process_single_tool(
        tool_call=tool_call,
        db="db",
        profile=SimpleNamespace(id=1),
        cfg=cfg,
        messages=[],
        username="tester",
        session_id="session_1",
        turn=1,
        uid="user_1",
        allowed_knowledge_base_ids=[1, 2],
    )
    payload = json.loads(tool_msg.content)

    assert payload["status"] == "queued"
    assert payload["tool_name"] == "firecrawl_search"
    assert payload["task_id"] == 42
    assert submitted["db"] == "db"
    assert submitted["arguments"] == {"query": "mono"}
    assert submitted["tool_call_id"] == "call_bg"
    assert submitted["allowed_knowledge_base_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_process_single_tool_ignores_background_for_disabled_tool(monkeypatch):
    _patch_process_single_tool_side_effects(monkeypatch)

    async def fail_audit(*args, **kwargs):
        raise AssertionError("audit should not run for disabled tools")

    monkeypatch.setattr(process_single_tool_module, "audit_tool_call", fail_audit)

    tool_call = SimpleNamespace(id="call_disabled_bg", name="firecrawl_search", arguments={"query": "mono", "run_in_background": True})
    cfg = SimpleNamespace(tool=SimpleNamespace(enabled_tools=[]))

    tool_msg = await process_single_tool(
        tool_call=tool_call,
        db=None,
        profile=SimpleNamespace(id=1),
        cfg=cfg,
        messages=[],
        username="tester",
        session_id="session_1",
        turn=1,
        uid="user_1",
    )
    payload = json.loads(tool_msg.content)

    assert payload["status"] == "failed"
    assert payload["tool_name"] == "firecrawl_search"
    assert "not enabled" in payload["error"]
