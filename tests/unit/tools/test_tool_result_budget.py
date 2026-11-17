import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.constants import SHELL_TOOL_NAME, TOOL_RESULT_COMPACT_TRUNCATION_NOTICE, TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
from app.core.i18n.context import reset_current_log_locale, set_current_log_locale
from app.core.tools.knowledge_base_query import KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA
from app.core.utils.dispatcher import process_single_tool as process_single_tool_module
from app.core.utils.dispatcher import truncate_tool_result as truncate_tool_result_module
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile, ProfileConfig
from tests.memory_recall_support import memory_recall_payload
from tests.unit.response_test_support import knowledge_query_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_process_single_tool_truncation_log_uses_localized_per_tool_budget(monkeypatch, tmp_path, locale):
    payload = {
        "stdout": "large-shell-result-" * 400,
        "tail_evidence": "LOG-TAIL-EVIDENCE",
    }
    original_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    cfg = ProfileConfig.model_validate(
        {
            "memory": {"enabled": True},
            "tool": {"enabled_tools": [SHELL_TOOL_NAME]},
        }
    )
    profile = Profile(id=1, uid="user-1", name="profile", configs=cfg.model_dump(mode="json"))
    tool_call = SimpleNamespace(
        id="call-large-result",
        name=SHELL_TOOL_NAME,
        arguments={"command": "echo large", "execution_mode": "interactive"},
    )
    warnings = []

    class CapturingLogger:
        def bind(self, **_kwargs):
            return self

        def warning(self, message):
            warnings.append(message)

    logger = CapturingLogger()
    monkeypatch.setitem(process_single_tool_module.TOOL_EXECUTOR_MAP, SHELL_TOOL_NAME, _build_fake_executor(original_content))
    monkeypatch.setattr(process_single_tool_module, "ensure_session_reply_work_claim", AsyncMock())
    monkeypatch.setattr(process_single_tool_module, "get_logger", lambda _name: logger)
    monkeypatch.setattr(process_single_tool_module, "get_user_temp_dir", lambda _root, _uid: tmp_path)

    locale_token = set_current_log_locale(locale)
    try:
        await process_single_tool_module.process_single_tool(
            tool_call,
            db=SimpleNamespace(),
            profile=profile,
            cfg=cfg,
            messages=[],
            username="user",
            session_id="session-1",
            turn=1,
            uid="user-1",
            context_window_k=1,
        )
    finally:
        reset_current_log_locale(locale_token)

    assert len(warnings) == 1
    warning = warnings[0]
    if locale == "zh":
        assert "单工具预算 500 tokens" in warning
    else:
        assert "per-tool budget=500 tokens" in warning
    assert "context_window_k=1" in warning
    spill_files = list((tmp_path / "tool_results").glob("tool_result_*.txt"))
    assert len(spill_files) == 1
    assert spill_files[0].read_text(encoding="utf-8") == original_content


def _build_fake_executor(server_result):
    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, **_kwargs):
            return server_result

    return FakeExecutor


async def _process_fake_executor_result(
    monkeypatch,
    tmp_path,
    *,
    tool_name,
    arguments,
    payload,
):
    server_result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    received_arguments = []

    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, **kwargs):
            received_arguments.append(kwargs)
            return server_result

    monkeypatch.setitem(process_single_tool_module.TOOL_EXECUTOR_MAP, tool_name, FakeExecutor)
    monkeypatch.setattr(process_single_tool_module, "ensure_session_reply_work_claim", AsyncMock())
    monkeypatch.setattr(process_single_tool_module, "get_user_temp_dir", lambda _root, _uid: tmp_path)

    cfg = ProfileConfig.model_validate(
        {
            "memory": {"enabled": tool_name != process_single_tool_module.KNOWLEDGE_BASE_QUERY_TOOL_NAME},
            "tool": {"enabled_tools": [tool_name]},
        }
    )
    profile = Profile(id=1, uid="user-1", name="profile", configs=cfg.model_dump(mode="json"))
    tool_call = SimpleNamespace(id="call-test", name=tool_name, arguments=arguments)
    if tool_name == process_single_tool_module.KNOWLEDGE_BASE_QUERY_TOOL_NAME:
        tool_schemas = [KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA]
    else:
        tool_schemas = None
    tool_result = await process_single_tool_module.process_single_tool(
        tool_call,
        db=SimpleNamespace(),
        profile=profile,
        cfg=cfg,
        messages=[],
        username="user",
        session_id="session-1",
        turn=1,
        uid="user-1",
        context_window_k=1,
        tool_schemas=tool_schemas,
    )
    assert received_arguments == [arguments]
    return tool_result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            process_single_tool_module.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
            {"operation": "recall", "query": "topic"},
        ),
        (
            process_single_tool_module.KNOWLEDGE_BASE_QUERY_TOOL_NAME,
            {"knowledge_base_id": 1, "query": "topic"},
        ),
    ],
    ids=["managed-memory-recall", "query-knowledge-base"],
)
async def test_process_single_tool_preserves_recall_results_with_fixed_tool_capacity(
    monkeypatch,
    tmp_path,
    tool_name,
    arguments,
):
    first_text = "memory-result-" * 400 + "UNIQUE-ITEMS-END"
    second_text = "second-memory-" * 400 + "UNIQUE-SECOND-ITEM-END"
    if tool_name == process_single_tool_module.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME:
        payload = memory_recall_payload()
        payload["memories"][0]["content"] = first_text
        payload["memories"].append(
            {
                **payload["memories"][0],
                "memory_id": 2,
                "memory_key": "project.second",
                "content": second_text,
            }
        )

        managed_text = "managed-policy-" * 400 + "UNIQUE-MANAGED-END"
        user_text = "user-knowledge-" * 400 + "UNIQUE-USER-KNOWLEDGE-END"
        history_text = "conversation-history-" * 400 + "UNIQUE-CHAT-HISTORY-END"
        managed_knowledge = payload["knowledge_base"][0]
        managed_knowledge["knowledge_id"] = 101
        managed_knowledge["knowledge_key"] = "customer-preference"
        managed_knowledge["knowledge_expected_version"] = 3
        managed_knowledge["content"] = managed_text
        payload["knowledge_base"][1]["content"] = user_text
        payload["chat_history"][0]["content"] = history_text
        payload["chat_history"][1]["content"] = "assistant-history-" * 400 + "UNIQUE-ASSISTANT-HISTORY-END"
    else:
        payload = knowledge_query_payload([("guide.md", first_text), ("other.md", second_text)])
    original_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    content = await _process_fake_executor_result(
        monkeypatch,
        tmp_path,
        tool_name=tool_name,
        arguments=arguments,
        payload=payload,
    )

    assert content == original_content
    assert json.loads(content) == payload


@pytest.mark.asyncio
async def test_process_single_tool_preserves_managed_knowledge_order_and_full_text(monkeypatch, tmp_path):
    managed_text = "durable-memory-" * 400 + "UNIQUE-MIXED-MANAGED-END"
    payload = memory_recall_payload()
    payload["memories"] = []
    payload.pop("chat_history")

    user_knowledge_template = payload["knowledge_base"][1]
    managed_knowledge_template = payload["knowledge_base"][0]
    user_before = {**user_knowledge_template, "source": "user-before", "content": "First user result."}
    managed_knowledge = {
        **managed_knowledge_template,
        "source": "long-policy",
        "content": managed_text,
        "knowledge_id": 201,
        "knowledge_key": "long-policy",
        "knowledge_expected_version": 12,
    }
    user_after = {**user_knowledge_template, "source": "user-after", "content": "Last user result."}
    knowledge_base = [user_before, managed_knowledge, user_after]
    payload["knowledge_base"] = knowledge_base

    content = await _process_fake_executor_result(
        monkeypatch,
        tmp_path,
        tool_name=process_single_tool_module.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
        arguments={"operation": "recall", "query": "topic"},
        payload=payload,
    )
    result = json.loads(content)

    assert result["knowledge_base"] == knowledge_base
    assert result["knowledge_base"][1]["content"] == managed_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "source_type"),
    [
        (
            SHELL_TOOL_NAME,
            {"command": "echo test", "execution_mode": "interactive"},
            "managed_knowledge",
        ),
        (
            process_single_tool_module.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
            {
                "operation": "create",
                "content": "Remember this personal preference.",
                "memory_key": "preference",
                "memory_type": "preference",
            },
            "user_memory",
        ),
        (
            process_single_tool_module.MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
            {
                "operation": "knowledge_create",
                "knowledge_key": "deployment-policy",
                "knowledge_content": "Deploy only after approval.",
            },
            "managed_knowledge",
        ),
    ],
    ids=["managed-label-from-shell", "personal-memory-create", "managed-knowledge-create"],
)
async def test_process_single_tool_truncates_non_exempt_knowledge_results(
    monkeypatch,
    tmp_path,
    tool_name,
    arguments,
    source_type,
):
    long_text = "large-ordinary-result-" * 2_000
    payload = {
        "items": [{"source": "knowledge_base", "content": long_text}],
        "current_session_id": "session-1",
        "knowledge_base": [
            ({"source": "user_knowledge", "content": long_text} if source_type == "user_knowledge" else {"source_type": source_type, "content": long_text}),
        ],
        "chat_history": [{"role": "user", "content": long_text}],
    }
    original_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    content = await _process_fake_executor_result(
        monkeypatch,
        tmp_path,
        tool_name=tool_name,
        arguments=arguments,
        payload=payload,
    )

    json.loads(content)
    assert content != original_content
    assert long_text not in content
    assert len(content) < len(original_content)


@pytest.mark.asyncio
async def test_process_single_tool_keeps_fixed_capacity_result_independent_of_history_and_parallel_calls(monkeypatch, tmp_path):
    arguments = {"command": "echo fixed-capacity", "execution_mode": "interactive"}
    tail_evidence = "FIXED-CAPACITY-TAIL-EVIDENCE"
    payload = {
        "status": "success",
        "stdout": "fixed-capacity-result-" * 350,
        "tail_evidence": tail_evidence,
    }
    server_result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    received_arguments = []

    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, **kwargs):
            received_arguments.append(kwargs)
            return server_result

    monkeypatch.setitem(process_single_tool_module.TOOL_EXECUTOR_MAP, SHELL_TOOL_NAME, FakeExecutor)
    monkeypatch.setattr(process_single_tool_module, "ensure_session_reply_work_claim", AsyncMock())
    monkeypatch.setattr(process_single_tool_module, "get_user_temp_dir", lambda _root, _uid: tmp_path)

    cfg = ProfileConfig.model_validate(
        {
            "memory": {"enabled": True},
            "tool": {"enabled_tools": [SHELL_TOOL_NAME]},
        }
    )
    profile = Profile(id=1, uid="user-1", name="profile", configs=cfg.model_dump(mode="json"))
    parallel_intent_history = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(id="history-call-1", name=SHELL_TOOL_NAME, arguments=arguments),
                InternalToolCall(id="history-call-2", name=SHELL_TOOL_NAME, arguments=arguments),
            ],
        )
    ]
    histories = [
        [],
        [InternalMessage(role=MessageRole.USER, content="large-history-" * 1_000)],
        parallel_intent_history,
    ]

    history_results = []
    for index, messages in enumerate(histories):
        result = await process_single_tool_module.process_single_tool(
            SimpleNamespace(id=f"history-result-{index}", name=SHELL_TOOL_NAME, arguments=arguments),
            db=SimpleNamespace(),
            profile=profile,
            cfg=cfg,
            messages=messages,
            username="user",
            session_id="session-1",
            turn=1,
            uid="user-1",
            context_window_k=8,
        )
        history_results.append(result.content)

    parallel_results = await asyncio.gather(
        *[
            process_single_tool_module.process_single_tool(
                SimpleNamespace(id=f"parallel-result-{index}", name=SHELL_TOOL_NAME, arguments=arguments),
                db=SimpleNamespace(),
                profile=profile,
                cfg=cfg,
                messages=parallel_intent_history,
                username="user",
                session_id="session-1",
                turn=1,
                uid="user-1",
                context_window_k=8,
            )
            for index in range(3)
        ]
    )
    parallel_contents = [result.content for result in parallel_results]

    assert history_results == [server_result] * len(histories)
    assert parallel_contents == [server_result] * 3
    assert all(json.loads(content)["tail_evidence"] == tail_evidence for content in history_results + parallel_contents)
    assert received_arguments == [arguments] * 6


def test_tool_result_truncation_uses_model_specific_encoding():
    content = "长期上下文 token estimation " * 200
    encoding = truncate_tool_result_module.tiktoken.get_encoding("o200k_base")
    expected_tokens = len(encoding.encode(content, disallowed_special=()))

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=100,
        limit_tokens=expected_tokens,
        model_id="gpt-5.6-luna",
        protocol="openai",
    )

    assert result.truncated is False
    assert result.original_tokens == expected_tokens
    assert result.final_tokens == expected_tokens


def test_untruncated_json_keeps_original_whitespace_and_real_token_stats():
    content = (
        json.dumps(
            {"status": "ready", "items": ["first", "second"]},
            ensure_ascii=False,
            indent=4,
        )
        + "\n"
    )
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    original_tokens = len(encoding.encode(content, disallowed_special=()))
    compact_tokens = len(
        encoding.encode(
            json.dumps(json.loads(content), ensure_ascii=False, separators=(",", ":")),
            disallowed_special=(),
        )
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=original_tokens,
    )

    assert compact_tokens < original_tokens
    assert result.content == content
    assert result.truncated is False
    assert result.original_tokens == original_tokens
    assert result.final_tokens == original_tokens


def test_overlimit_json_preserves_prefix_fields_and_truncates_string_inside_json(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "status": "ready",
        "request_id": "req-123",
        "details": "prefix-" + "segment-" * 400,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    original_tokens = len(encoding.encode(content, disallowed_special=()))
    fitting_prefix = "prefix-" + "segment-" * 8
    fitting_content = json.dumps(
        {
            "status": "ready",
            "request_id": "req-123",
            "details": fitting_prefix + notice,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = len(encoding.encode(fitting_content, disallowed_special=()))

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert result.original_tokens == original_tokens
    assert result.final_tokens == len(encoding.encode(result.content, disallowed_special=()))
    assert result.final_tokens <= limit_tokens
    assert truncated_payload["status"] == "ready"
    assert truncated_payload["request_id"] == "req-123"
    assert truncated_payload["details"].startswith(fitting_prefix)
    assert truncated_payload["details"].endswith(notice)
    assert len(truncated_payload["details"]) < len(payload["details"])


def test_overlimit_json_keeps_later_object_fields_when_earlier_value_is_large(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "stdout": "output-" * 400,
        "stderr": "fatal warning",
        "exit_code": 7,
        "system_info": "windows",
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    expected_tail = json.dumps(
        {
            "stdout": notice,
            "stderr": payload["stderr"],
            "exit_code": payload["exit_code"],
            "system_info": payload["system_info"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = len(encoding.encode(expected_tail, disallowed_special=())) + 20

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert result.truncated is True
    assert list(truncated_payload) == list(payload)
    assert truncated_payload["stdout"].endswith(notice)
    assert truncated_payload["stderr"] == payload["stderr"]
    assert truncated_payload["exit_code"] == payload["exit_code"]
    assert truncated_payload["system_info"] == payload["system_info"]
    assert result.final_tokens <= limit_tokens


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "x" * 200},
        ["x" * 200],
        "x" * 200,
        10**100,
    ],
)
def test_tiny_json_budget_returns_parseable_json_for_each_value_shape(payload):
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=1,
    )

    json.loads(result.content)
    assert result.truncated is True
    assert result.final_tokens == len(encoding.encode(result.content, disallowed_special=()))
    assert result.final_tokens <= 1


def test_json_truncation_fallback_uses_character_estimates_and_stays_parseable(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "status": "ready",
        "message": "前缀" + "中文内容" * 300,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    original_tokens = truncate_tool_result_module._estimate_tokens_by_chars(content)
    fitting_content = json.dumps(
        {"status": "ready", "message": "前缀" + notice},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = max(1, truncate_tool_result_module._estimate_tokens_by_chars(fitting_content))

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert truncated_payload["status"] == "ready"
    assert notice in truncated_payload["message"]
    assert result.original_tokens == original_tokens
    assert result.final_tokens == truncate_tool_result_module._estimate_tokens_by_chars(result.content)
    assert result.final_tokens <= limit_tokens


def test_character_estimate_fallback_keeps_large_chinese_non_json_within_budget(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    content = "这是大量中文内容，用于验证字符估算回退。" * 300
    original_tokens = truncate_tool_result_module._estimate_tokens_by_chars(content)
    limit_tokens = max(
        truncate_tool_result_module._estimate_tokens_by_chars(notice) + 1,
        original_tokens // 4,
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert result.original_tokens == original_tokens
    assert result.final_tokens == truncate_tool_result_module._estimate_tokens_by_chars(result.content)
    assert result.final_tokens <= limit_tokens
    assert notice in result.content
    assert len(result.content) < len(content)


def test_truncated_tool_result_uses_budget_safe_notice_when_full_notice_does_not_fit(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=1,
    )

    assert result.truncated is True
    assert result.content == TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
    assert result.final_tokens <= 1


def test_truncated_tool_result_fallback_uses_budget_safe_notice_when_full_notice_does_not_fit(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=1,
    )

    assert result.truncated is True
    assert result.content == TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
    assert result.final_tokens <= 1


def test_compact_truncation_notice_is_shorter_than_full_notice(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=2,
    )

    assert result.content == TOOL_RESULT_COMPACT_TRUNCATION_NOTICE
