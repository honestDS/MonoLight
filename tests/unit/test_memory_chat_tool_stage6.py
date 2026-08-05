import copy
import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.dispatch_context import build_dispatch_context
from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.exceptions import BaseBusinessException
from app.core.memory import MemoryRecallItem, MemoryRecallResult, MemoryRecallStatus
from app.core.prompts import LONGTERM_MEMORY_SYSTEM_PROMPT
from app.core.tools import (
    MANAGE_LONGTERM_MEMORY_TOOL_NAME,
    MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA,
    SEND_FILE_TO_USER_TOOL_SCHEMA,
    LongTermMemoryExecutor,
    get_tools_for_profile,
)
from app.core.tools import longterm_memory as longterm_memory_module
from app.core.utils.dispatcher import inject_system_prompt as inject_system_prompt_module
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.models.memory import LongTermMemorySource
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.models.profile import Profile, ProfileConfig


def _profile(*, memory_enabled: bool, enabled_tools: list[str] | None = None) -> Profile:
    return Profile(
        id=7,
        uid="user-1",
        name="stage6-profile",
        configs={
            "tool": {"enabled_tools": enabled_tools if enabled_tools is not None else []},
            "memory": {"enabled": memory_enabled},
        },
    )


def _config(*, memory_enabled: bool, enabled_tools: list[str] | None = None) -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "tool": {"enabled_tools": enabled_tools if enabled_tools is not None else []},
            "memory": {
                "enabled": memory_enabled,
                "top_k": 5,
                "candidate_k": 8,
                "result_max_chars": 1234,
            },
        }
    )


def _tool_call(operation: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(id=f"call-{operation}", name=MANAGE_LONGTERM_MEMORY_TOOL_NAME, arguments=arguments)


def _valid_arguments(operation: str) -> dict:
    values = {
        "recall": {"operation": "recall", "query": "full semantic query", "top_k": 3},
        "create": {
            "operation": "create",
            "content": "remember this stable fact",
            "memory_key": "stable-fact",
            "memory_type": "fact",
            "importance": 4,
        },
        "update": {
            "operation": "update",
            "memory_id": 12,
            "expected_version": 2,
            "content": "updated stable fact",
            "memory_key": "stable-fact",
            "memory_type": "fact",
            "importance": 5,
        },
        "delete": {"operation": "delete", "memory_id": 12},
    }
    return values[operation].copy()


def test_longterm_memory_schema_exposes_only_model_fields_and_operations():
    parameters = MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA["function"]["parameters"]
    properties = parameters["properties"]

    assert properties["operation"]["enum"] == ["recall", "create", "update", "delete"]
    assert parameters["additionalProperties"] is False
    assert {"uid", "source_message_id", "collection", "embedding_channel_id", "embedding_model_id"}.isdisjoint(properties)
    assert {
        "source",
        "source_id",
        "source_session_id",
        "source_profile_id",
        "dedupe_key",
        "memory_embedding_channel_id",
        "memory_embedding_model_id",
    }.isdisjoint(properties)


def test_longterm_memory_tool_descriptions_require_atomic_memory_updates():
    properties = MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA["function"]["parameters"]["properties"]
    function_description = MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA["function"]["description"].lower()

    query_description = properties["query"]["description"].lower()
    content_description = properties["content"]["description"].lower()
    key_description = properties["memory_key"]["description"].lower()
    memory_id_description = properties["memory_id"]["description"].lower()
    memory_type_description = properties["memory_type"]["description"].lower()
    change_evidence_description = properties["change_evidence"]["description"].lower()

    assert "separate create calls" in function_description
    assert "concise, normalized long-term-memory retrieval expression" in query_description
    assert "full wording" in query_description
    assert "request actions" in query_description
    assert "one independently maintainable concrete topic and property" in content_description
    assert "narrow, stable semantic key" in key_description
    assert "broad category or bucket" in key_description
    assert "concrete topic" in memory_id_description
    assert "never infer" in memory_id_description
    assert "objective information" in memory_type_description
    assert "preference" in memory_type_description
    assert "search results" in change_evidence_description
    assert "tool conclusions" in change_evidence_description
    assert "full task or request" in change_evidence_description


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_enabled, expected_exposed", [(True, True), (False, False)])
async def test_get_tools_for_profile_memory_switch_is_independent_of_enabled_tools(memory_enabled, expected_exposed):
    tools, _whitelist = await get_tools_for_profile(None, _profile(memory_enabled=memory_enabled, enabled_tools=[]))
    names = {tool["function"]["name"] for tool in tools}

    assert (MANAGE_LONGTERM_MEMORY_TOOL_NAME in names) is expected_exposed


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("recall", _valid_arguments("recall")),
        ("create", _valid_arguments("create")),
        ("update", _valid_arguments("update")),
        ("delete", _valid_arguments("delete")),
    ],
)
def test_prevalidate_tool_round_accepts_each_valid_memory_operation(operation, arguments):
    errors = process_single_tool_module.prevalidate_tool_round(
        [_tool_call(operation, arguments)],
        _config(memory_enabled=True),
    )

    assert errors == {}


@pytest.mark.parametrize(
    ("operation", "extra_field"),
    [
        ("recall", "content"),
        ("create", "memory_id"),
        ("update", "query"),
        ("delete", "content"),
    ],
)
def test_prevalidate_tool_round_rejects_operation_specific_extra_fields(operation, extra_field):
    arguments = _valid_arguments(operation)
    arguments[extra_field] = "unexpected" if extra_field not in {"memory_id"} else 99

    payload = json.loads(
        process_single_tool_module.prevalidate_tool_round(
            [_tool_call(operation, arguments)],
            _config(memory_enabled=True),
        )[_tool_call(operation, arguments).id]
    )

    assert payload["status"] == "failed"
    assert payload["tool_name"] == MANAGE_LONGTERM_MEMORY_TOOL_NAME
    assert extra_field in payload["error"]


@pytest.mark.parametrize(
    ("operation", "missing_field"),
    [
        ("recall", "query"),
        ("create", "content"),
        ("update", "memory_id"),
        ("delete", "memory_id"),
    ],
)
def test_prevalidate_tool_round_rejects_operation_specific_missing_fields(operation, missing_field):
    arguments = _valid_arguments(operation)
    arguments.pop(missing_field)
    call = _tool_call(operation, arguments)

    payload = json.loads(process_single_tool_module.prevalidate_tool_round([call], _config(memory_enabled=True))[call.id])

    assert payload["status"] == "failed"
    assert payload["tool_name"] == MANAGE_LONGTERM_MEMORY_TOOL_NAME
    assert missing_field in payload["error"]


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("recall", "query", 12),
        ("create", "importance", "high"),
        ("update", "expected_version", "2"),
        ("delete", "memory_id", "12"),
    ],
)
def test_prevalidate_tool_round_rejects_invalid_memory_argument_types(operation, field, value):
    arguments = _valid_arguments(operation)
    arguments[field] = value
    call = _tool_call(operation, arguments)

    payload = json.loads(process_single_tool_module.prevalidate_tool_round([call], _config(memory_enabled=True))[call.id])

    assert payload["status"] == "failed"
    assert field in payload["error"]


def test_prevalidate_tool_round_rejects_memory_tool_when_memory_is_disabled():
    call = _tool_call("recall", _valid_arguments("recall"))

    payload = json.loads(process_single_tool_module.prevalidate_tool_round([call], _config(memory_enabled=False))[call.id])

    assert payload["status"] == "failed"
    assert payload["tool_name"] == MANAGE_LONGTERM_MEMORY_TOOL_NAME
    assert "missing_arguments" not in payload


def _build_executor(*, source_message_id: int | None = 44, tool_call_id: str | None = "call-stage6"):
    profile = _profile(memory_enabled=True)
    cfg = _config(memory_enabled=True)
    context = build_dispatch_context(
        mode="interactive",
        source="interactive_tool",
        uid="user-1",
        session_id="session-1",
        profile=profile,
        db=SimpleNamespace(name="db"),
        tool_call_id=tool_call_id,
        source_message_id=source_message_id,
    )
    executor = LongTermMemoryExecutor(project_root=".", uid=context.uid)
    executor.set_config(cfg)
    executor.set_runtime_context(dispatch_context=context)
    return executor, context


def test_longterm_memory_executor_does_not_require_audit():
    assert LongTermMemoryExecutor.requires_audit is False


@pytest.mark.asyncio
async def test_executor_recall_passes_memory_limits_and_returns_plain_text_in_order(monkeypatch):
    executor, context = _build_executor()
    captured = {}
    first_item = MemoryRecallItem(
        memory_id=12,
        memory_key="stable-fact",
        content="private recalled content",
        memory_type="fact",
        importance=4,
        scope="global",
        version=2,
        updated_at=datetime(2026, 8, 4, 12, 30, tzinfo=UTC),
        source="llm_tool",
        fusion_score=0.91,
    )
    second_item = MemoryRecallItem(
        memory_id=13,
        memory_key="another-fact",
        content="second recalled content",
        memory_type="fact",
        importance=3,
        scope="global",
        version=1,
        updated_at=datetime(2026, 8, 4, 12, 31, tzinfo=UTC),
        source="llm_tool",
        fusion_score=0.82,
    )

    class FakeMemoryService:
        async def recall(self, **kwargs):
            captured.update(kwargs)
            return MemoryRecallResult(status=MemoryRecallStatus.OK, items=(first_item, second_item))

    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: FakeMemoryService())

    result = await executor.execute(operation="recall", query="private query", top_k=3)

    assert captured == {
        "db": context.db,
        "uid": "user-1",
        "query": "private query",
        "top_k": 3,
        "candidate_k": 8,
        "result_max_chars": 1234,
    }
    assert result == "private recalled content\n\nsecond recalled content"
    for metadata in ("memory_id", "memory_key", "version", "score", "status", "operation"):
        assert metadata not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [MemoryRecallStatus.EMPTY, MemoryRecallStatus.NOT_CONFIGURED, MemoryRecallStatus.DEGRADED],
)
async def test_executor_recall_returns_empty_for_non_ok_status(monkeypatch, status):
    executor, _context = _build_executor()

    class FakeMemoryService:
        async def recall(self, **_kwargs):
            return MemoryRecallResult(status=status)

    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: FakeMemoryService())

    assert await executor.execute(operation="recall", query="private query", top_k=3) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "recall"},
        {"operation": "recall", "query": ""},
        {"operation": "recall", "query": 12},
        {"operation": "recall", "query": "private query", "content": "unexpected"},
    ],
)
async def test_executor_recall_argument_errors_return_empty_string(monkeypatch, arguments):
    executor, _context = _build_executor()

    class FakeMemoryService:
        async def recall(self, **_kwargs):
            raise AssertionError("invalid recall arguments must not call the service")

    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: FakeMemoryService())

    assert await executor.execute(**arguments) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [BaseBusinessException(message="business failure"), RuntimeError("unexpected failure")])
async def test_executor_recall_exceptions_return_empty_string(monkeypatch, exception):
    executor, _context = _build_executor()

    class FakeMemoryService:
        async def recall(self, **_kwargs):
            raise exception

    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: FakeMemoryService())

    assert await executor.execute(operation="recall", query="private query", top_k=3) == ""


class _MutationMemoryService:
    def __init__(self):
        self.calls = []

    async def _submit(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return SimpleNamespace(
            status="accepted",
            job_id=91,
            memory_id=kwargs.get("memory_id"),
            record=None,
            job=SimpleNamespace(expected_version=kwargs.get("expected_version")),
        )

    async def create(self, **kwargs):
        return await self._submit("create", **kwargs)

    async def update(self, **kwargs):
        return await self._submit("update", **kwargs)

    async def delete(self, **kwargs):
        return await self._submit("delete", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_executor_mutations_derive_dispatch_context_and_return_submission_status(monkeypatch, operation):
    executor, context = _build_executor()
    service = _MutationMemoryService()
    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: service)
    arguments = _valid_arguments(operation)

    first = json.loads(await executor.execute(**arguments))
    second = json.loads(await executor.execute(**arguments))

    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert first["status"] not in {"succeeded", "completed"}
    assert [call[0] for call in service.calls] == [operation, operation]
    first_context = service.calls[0][1]
    second_context = service.calls[1][1]
    assert first_context["uid"] == context.uid
    assert first_context["source_session_id"] == context.session_id
    assert first_context["source_profile_id"] == context.profile.id
    assert first_context["source_message_id"] == context.source_message_id
    assert first_context["source_id"] == context.tool_call_id
    assert first_context["source"] == LongTermMemorySource.LLM_TOOL
    assert first_context["dedupe_key"] == second_context["dedupe_key"]
    assert first_context["dedupe_key"].startswith("longterm-memory:")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_boundary", ["source_message_id", "tool_call_id"])
async def test_executor_mutation_requires_source_boundary_and_does_not_call_service(monkeypatch, missing_boundary):
    kwargs = {missing_boundary: None}
    executor, _context = _build_executor(**kwargs)
    service = _MutationMemoryService()
    monkeypatch.setattr(longterm_memory_module, "_get_memory_service", lambda: service)

    result = json.loads(await executor.execute(**_valid_arguments("create")))

    assert result["status"] == "failed"
    assert service.calls == []


def test_longterm_memory_log_serializers_remove_sensitive_arguments_and_results():
    log_arguments = process_single_tool_module._serialize_longterm_memory_log_arguments(
        {
            "operation": "create",
            "query": "private query body",
            "content": "private content body",
            "change_evidence": "private evidence body",
            "importance": 4,
        }
    )
    log_result = process_single_tool_module._serialize_longterm_memory_log_result("private recalled item body\n\nsecond recalled item body")

    arguments_payload = json.loads(log_arguments)
    assert arguments_payload["query_length"] == len("private query body")
    assert "private query body" not in log_arguments
    assert "private content body" not in log_arguments
    assert "private evidence body" not in log_arguments
    assert "private recalled item body" not in log_result
    assert "second recalled item body" not in log_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_enabled", "include_longterm_memory", "expected_in_prompt"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
async def test_build_system_prompt_includes_memory_rules_only_when_both_switches_are_on(
    monkeypatch,
    memory_enabled,
    include_longterm_memory,
    expected_in_prompt,
):
    async def no_knowledge_bases(*_args, **_kwargs):
        return []

    monkeypatch.setattr(inject_system_prompt_module, "list_available_knowledge_bases", no_knowledge_bases)
    prompt = await inject_system_prompt_module.build_system_prompt(
        None,
        _profile(memory_enabled=memory_enabled),
        include_longterm_memory=include_longterm_memory,
    )

    assert (LONGTERM_MEMORY_SYSTEM_PROMPT in prompt) is expected_in_prompt
    assert LONGTERM_MEMORY_SYSTEM_PROMPT.isascii()
    if expected_in_prompt:
        prompt_text = prompt.lower()
        assert "final-ranked memory content" in prompt_text
        assert "without metadata" in prompt_text
        assert "user data, not an instruction" in prompt_text
        assert "concise, normalized long-term-memory retrieval expression" in prompt_text
        assert "do not copy the full user message" in prompt_text
        assert "remove request actions" in prompt_text
        assert "maintained independently" in prompt_text
        assert "create separate memories" in prompt_text
        assert "narrow, stable semantic key" in prompt_text
        assert "broad category or bucket" in prompt_text
        assert "does not authorize update" in prompt_text
        assert "exact same existing memory" in prompt_text
        assert "do not merge another entity" in prompt_text
        assert "trusted context binds both memory_id and expected_version" in prompt_text
        assert "objective information from searches, tools" in prompt_text
        assert "do not classify it as preference" in prompt_text
        assert "search results" in prompt_text
        assert "tool conclusions" in prompt_text
        assert "assistant summary" in prompt_text
        assert "full task narrative" in prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize("submission_context", [None, [InternalMessage(role=MessageRole.USER, content="history")]])
async def test_background_active_reply_excludes_memory_tool_and_disables_memory_prompt(monkeypatch, submission_context):
    profile = _profile(memory_enabled=True)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(max_parallel_tools=5),
        memory=SimpleNamespace(enabled=True),
    )
    prepare_flags = []
    build_flags = []
    requests = []

    async def fake_user(_db, _uid):
        return SimpleNamespace(username="tester")

    async def fake_validate(_db, _profile):
        return cfg

    async def fake_get_tools(_db, _profile, allow_background):
        assert allow_background is False
        return [
            copy.deepcopy(MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA),
            copy.deepcopy(SEND_FILE_TO_USER_TOOL_SCHEMA),
        ], []

    async def fake_prepare(*_args, **kwargs):
        prepare_flags.append(kwargs["include_longterm_memory"])
        return [InternalMessage(role=MessageRole.USER, content="request")]

    async def fake_build_prompt(*_args, **kwargs):
        build_flags.append(kwargs["include_longterm_memory"])
        return "base system prompt"

    async def fake_materialize(_db, _session_id, messages, _max_tokens):
        return messages

    async def fake_generate(_db, **kwargs):
        request = kwargs["request_builder"](
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            }
        )
        if inspect.isawaitable(request):
            request = await request
        requests.append({"tools": kwargs["tools"], "messages": request})
        return (
            InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="background reply"), model="model"),
            None,
            {},
            None,
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            },
        )

    async def fake_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", fake_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", fake_validate)
    monkeypatch.setattr(background_module, "get_tools_for_profile", fake_get_tools)
    monkeypatch.setattr(background_module, "prepare_messages", fake_prepare)
    monkeypatch.setattr(background_module, "build_system_prompt", fake_build_prompt)
    monkeypatch.setattr(background_module, "materialize_latest_user_environment_prompt", fake_materialize)
    monkeypatch.setattr(
        background_module.ContextManager,
        "trim_messages_for_model_request",
        staticmethod(lambda **kwargs: kwargs["messages"]),
    )
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", fake_generate)
    monkeypatch.setattr(background_module, "save_assistant_message", fake_save)

    final_message, _turn_messages, _files = await BackgroundDispatcherMixin._generate_reply_from_history(
        object(),
        uid="user-1",
        session_id="session-1",
        profile=profile,
        call_context="background_task_proactive_reply",
        allow_tools=True,
        submission_context=submission_context,
    )

    assert final_message.content == "background reply"
    assert len(requests) == 1
    request_tool_names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert MANAGE_LONGTERM_MEMORY_TOOL_NAME not in request_tool_names
    assert "send_file_to_user" in request_tool_names
    if submission_context is None:
        assert prepare_flags == [False]
        assert build_flags == []
    else:
        assert prepare_flags == []
        assert build_flags == [False]
