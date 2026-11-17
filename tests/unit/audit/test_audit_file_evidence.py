import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.core.audit.context_budget import (
    AUDIT_READ_TEXT_FILE_TOOL_SCHEMA,
    build_read_tool_message,
    fit_file_tool_payload_to_context,
    fit_read_result_to_context,
    get_audit_tool_schemas,
    max_input_tokens,
    read_token_budget,
)
from app.core.audit.service import (
    AuditReadBudget,
    _call_auditor,
    _collect_file_mutation_snapshots,
    _execute_audit_read_tool_call,
    _file_checks_are_sufficient,
    _file_snapshots_from_reads,
    _read_for_audit_sync,
    _requires_confirmation_from_evidence,
    _summarize_pending,
)
from app.core.constants import DEFAULT_CHAT_MAX_TOKENS
from app.core.i18n import t
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_STANDARD_BATCH_PROMPT, AUDIT_TASK_AUTHORIZATION_PROMPT
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.providers.llm.client import estimate_request_context_tokens
from tests.unit.audit.test_audit_confirmation_service import _auditor_response_context, _profile_config


def test_audit_file_reader_accepts_any_path_but_requires_round_tool_id(tmp_path):
    outside_file = tmp_path.parent / "audit-anywhere.txt"
    outside_file.write_text("unrestricted evidence", encoding="utf-8")

    result = _read_for_audit_sync(str(outside_file), "call-1", {"call-1"}, tmp_path, 100)
    invalid = _read_for_audit_sync(str(outside_file), "other-call", {"call-1"}, tmp_path, 100)

    assert result["status"] == "ok"
    assert result["content"] == "unrestricted evidence"
    assert result["tool_call_id"] == "call-1"
    assert invalid["status"] == "invalid"


def test_file_checks_are_bound_to_the_actual_read_snapshot(tmp_path):
    source = tmp_path / "entry.txt"
    source.write_text("evidence", encoding="utf-8")
    read = _read_for_audit_sync(str(source), "call-1", {"call-1"}, tmp_path, 100)
    snapshots = _file_snapshots_from_reads([read], {"call-1"})["call-1"]
    valid_check = {key: read[key] for key in ("original_path", "absolute_path", "resolved_path", "exists", "file_type", "status", "size", "sha256", "truncated", "bytes_read")}

    assert snapshots[0]["content"] == read["content"]
    assert snapshots[0]["bytes_read"] == read["bytes_read"]
    assert snapshots[0]["tool_call_id"] == read["tool_call_id"]
    assert "content" not in valid_check
    assert _file_checks_are_sufficient(snapshots, [read], [valid_check])
    assert not _file_checks_are_sufficient(snapshots, [read], [{**valid_check, "sha256": "0" * 64}])
    assert not _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), snapshots, [read], [valid_check])
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), snapshots, [read], [])
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [], [valid_check])
    assert not _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [], [])


def test_truncated_file_check_with_matching_metadata_does_not_require_confirmation(tmp_path):
    source = tmp_path / "entry.txt"
    source.write_text("alpha beta gamma delta", encoding="utf-8")
    tool_call = InternalToolCall(id="call-1", name="execute_shell", arguments={})
    read = _read_for_audit_sync(str(source), "call-1", {"call-1"}, tmp_path, 1)
    snapshots = _file_snapshots_from_reads([read], {"call-1"})["call-1"]
    valid_check = {key: read[key] for key in ("original_path", "absolute_path", "resolved_path", "exists", "file_type", "status", "size", "sha256", "truncated", "bytes_read")}

    assert read["status"] == "ok"
    assert read["truncated"] is True
    assert not _requires_confirmation_from_evidence(tool_call, snapshots, [read], [valid_check])
    assert _requires_confirmation_from_evidence(tool_call, snapshots, [read], [{**valid_check, "bytes_read": valid_check["bytes_read"] + 1}])
    assert _requires_confirmation_from_evidence(tool_call, snapshots, [{**read, "status": "unreadable", "content": None}], [valid_check])


def testread_token_budget_shrinks_as_messages_grow():
    chat_params = {"context_window_k": 8, "max_tokens": 512}
    messages = [InternalMessage(role=MessageRole.SYSTEM, content="audit")]

    initial_budget, initial_context_tokens = read_token_budget(messages, chat_params)
    messages.append(InternalMessage(role=MessageRole.USER, content="evidence " * 500))
    later_budget, later_context_tokens = read_token_budget(messages, chat_params)

    assert later_context_tokens > initial_context_tokens
    assert later_budget < initial_budget


def testmax_input_tokens_uses_proportional_safety_margin_with_minimum():
    assert max_input_tokens({"context_window_k": 8, "max_tokens": 512}) == 8000 - 512 - 800
    assert max_input_tokens({"context_window_k": 1, "max_tokens": 512}) == 1000 - 512 - 256


def test_small_file_tool_write_audit_payload_is_unchanged_without_argument_evidence():
    payload = {
        "confirmation_threshold": 5,
        "working_directory": "C:/audit",
        "tool_calls": [
            {
                "tool_call_id": "call-1",
                "tool_name": "file_tool",
                "arguments": {"operation": "write", "path": "note.txt", "content": "short content"},
            }
        ],
    }

    adapted_payload = fit_file_tool_payload_to_context(
        "audit",
        payload,
        {"context_window_k": 2, "max_tokens": 256},
    )

    assert adapted_payload == payload
    assert adapted_payload is not payload
    assert "argument_evidence" not in adapted_payload["tool_calls"][0]


def test_large_file_tool_write_audit_payload_uses_prefix_evidence_within_input_budget():
    content = 'write this escaped value: \\"\\n' * 2000
    payload = {
        "confirmation_threshold": 5,
        "working_directory": "C:/audit",
        "tool_calls": [
            {
                "tool_call_id": "call-1",
                "tool_name": "file_tool",
                "arguments": {"operation": "write", "path": "note.txt", "content": content},
            }
        ],
    }
    chat_params = {"context_window_k": 2, "max_tokens": 256}

    adapted_payload = fit_file_tool_payload_to_context("audit", payload, chat_params)
    adapted_call = adapted_payload["tool_calls"][0]
    adapted_content = adapted_call["arguments"]["content"]
    evidence = adapted_call["argument_evidence"]["content"]
    request_tokens = estimate_request_context_tokens(
        [
            InternalMessage(role=MessageRole.SYSTEM, content="audit"),
            InternalMessage(role=MessageRole.USER, content=json.dumps(adapted_payload, ensure_ascii=False)),
        ],
        [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA],
    )

    assert payload["tool_calls"][0]["arguments"]["content"] == content
    assert content.startswith(adapted_content)
    assert evidence == {
        "status": "ok",
        "size": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "truncated": True,
        "bytes_read": len(adapted_content.encode("utf-8")),
    }
    assert request_tokens <= max_input_tokens(chat_params)


def test_multiple_file_tool_write_payloads_preserve_short_content_and_fairly_truncate_long_content():
    short_content = "short content " * 10
    long_content = "long content " * 4000
    payload = {
        "tool_calls": [
            {
                "tool_call_id": "short",
                "tool_name": "file_tool",
                "arguments": {"operation": "write", "path": "short.txt", "content": short_content},
            },
            {
                "tool_call_id": "long",
                "tool_name": "file_tool",
                "arguments": {"operation": "write", "path": "long.txt", "content": long_content},
            },
        ]
    }

    adapted_payload = fit_file_tool_payload_to_context(
        "audit",
        payload,
        {"context_window_k": 2, "max_tokens": 256},
    )
    short_call, long_call = adapted_payload["tool_calls"]

    assert short_call["arguments"]["content"] == short_content
    assert short_call["argument_evidence"]["content"]["truncated"] is False
    assert long_content.startswith(long_call["arguments"]["content"])
    assert long_call["argument_evidence"]["content"]["truncated"] is True


def test_large_file_tool_edit_and_patch_audit_payload_bounds_mutation_content():
    old_text = "old-value-" * 3000
    new_text = "new-value-" * 3000
    patch = "@@\n-" + ("old-" * 3000) + "\n+" + ("new-" * 3000)
    payload = {
        "tool_calls": [
            {
                "tool_call_id": "edit",
                "tool_name": "file_tool",
                "arguments": {
                    "operation": "edit",
                    "path": "note.txt",
                    "old_text": old_text,
                    "new_text": new_text,
                },
            },
            {
                "tool_call_id": "patch",
                "tool_name": "file_tool",
                "arguments": {
                    "operation": "patch",
                    "path": "note.txt",
                    "patch": patch,
                },
            },
        ]
    }

    adapted_payload = fit_file_tool_payload_to_context(
        "audit",
        payload,
        {"context_window_k": 2, "max_tokens": 256},
    )
    edit_call, patch_call = adapted_payload["tool_calls"]

    assert old_text.startswith(edit_call["arguments"]["old_text"])
    assert new_text.startswith(edit_call["arguments"]["new_text"])
    assert edit_call["argument_evidence"]["old_text"]["truncated"] is True
    assert edit_call["argument_evidence"]["new_text"]["truncated"] is True
    assert patch.startswith(patch_call["arguments"]["patch"])
    assert patch_call["argument_evidence"]["patch"]["truncated"] is True


@pytest.mark.asyncio
async def test_call_auditor_sends_adapted_file_tool_payload(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    calls = []
    content = "audit this write payload " * 5000

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content='{"results":[{"tool_call_id":"call-1","score":0,"reason":"safe","deny":false,"file_checks":[]}]}'),
            model="audit-model",
        )

    monkeypatch.setattr(service.channel_crud, "get", get_channel)
    monkeypatch.setattr(
        service,
        "resolve_chat_params",
        lambda *_args: {"context_window_k": 4, "temperature": 0.25, "top_p": 0.8, "max_tokens": 512, "chat_timeout": 30},
    )
    monkeypatch.setattr(service.LLMClient, "generate", generate)

    request_payload = {
        "confirmation_threshold": 5,
        "working_directory": str(tmp_path),
        "tool_calls": [
            {
                "tool_call_id": "call-1",
                "turn_index": 0,
                "tool_name": "file_tool",
                "arguments": {"operation": "write", "path": "note.txt", "content": content},
            }
        ],
    }
    request_context, response_context = await _call_auditor(FakeDb(), cfg, request_payload, tmp_path)

    expected_results = [{"tool_call_id": "call-1", "score": 0, "reason": "safe", "deny": False, "file_checks": []}]
    expected_response_context = {
        "content": {"results": expected_results},
        "file_reads": [],
        "shell_results": [],
        "parsed": {"results": expected_results},
    }
    assert {**response_context, "content": json.loads(response_context["content"])} == expected_response_context
    _, constructed_response_context = _auditor_response_context(expected_results)
    assert {**constructed_response_context, "content": json.loads(constructed_response_context["content"])} == expected_response_context
    messages = request_context["messages"]
    assert isinstance(messages, list)
    assert [message["role"] for message in messages[:2]] == ["system", "user"]

    sent_payload = json.loads(calls[0]["messages"][1].content)
    sent_call = sent_payload["tool_calls"][0]
    assert request_payload["tool_calls"][0]["arguments"]["content"] == content
    assert content.startswith(sent_call["arguments"]["content"])
    assert sent_call["argument_evidence"]["content"]["truncated"] is True
    assert calls[0]["temperature"] == 0.25
    assert calls[0]["top_p"] == 0.8


@pytest.mark.asyncio
async def test_pending_summary_sends_adapted_file_tool_payload(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_report_language = "en"
    calls = []
    content = "summarize this write payload " * 5000

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="Write note.txt after confirmation."),
            model="audit-model",
        )

    monkeypatch.setattr(service.channel_crud, "get", get_channel)
    monkeypatch.setattr(
        service,
        "resolve_chat_params",
        lambda *_args: {"context_window_k": 4, "temperature": 0.4, "top_p": 0.9, "max_tokens": 512, "chat_timeout": 30},
    )
    monkeypatch.setattr(service.LLMClient, "generate", generate)

    tool_calls = [{"id": "call-1", "name": "file_tool", "arguments": {"operation": "write", "path": "note.txt", "content": content}}]
    await _summarize_pending(FakeDb(), cfg, tool_calls, working_directory=tmp_path)

    sent_payload = json.loads(calls[0]["messages"][1].content)
    sent_call = sent_payload["tool_calls"][0]
    assert tool_calls[0]["arguments"]["content"] == content
    assert content.startswith(sent_call["arguments"]["content"])
    assert sent_call["argument_evidence"]["content"]["truncated"] is True
    assert calls[0]["temperature"] == 0.4
    assert calls[0]["top_p"] == 0.9


def test_audit_read_result_fits_escaped_json_within_input_budget():
    content = '"\\\n' * 2000
    read_result = {
        "path": "evidence.txt",
        "tool_call_id": "call-1",
        "original_path": "evidence.txt",
        "absolute_path": "C:/audit/evidence.txt",
        "resolved_path": "C:/audit/evidence.txt",
        "exists": True,
        "file_type": "regular_file",
        "size": len(content.encode("utf-8")),
        "sha256": "a" * 64,
        "status": "ok",
        "truncated": False,
        "content": content,
        "bytes_read": len(content.encode("utf-8")),
    }
    chat_params = {"context_window_k": 2, "max_tokens": 256}
    messages = [InternalMessage(role=MessageRole.SYSTEM, content="audit")]
    input_limit = max_input_tokens(chat_params)
    original_context_tokens = estimate_request_context_tokens(
        [*messages, build_read_tool_message("read-1", read_result)],
        [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA],
    )

    fitted_result = fit_read_result_to_context(messages, "read-1", read_result, chat_params)
    fitted_context_tokens = estimate_request_context_tokens(
        [*messages, build_read_tool_message("read-1", fitted_result)],
        [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA],
    )

    assert original_context_tokens > input_limit
    assert fitted_context_tokens <= input_limit
    assert content.startswith(fitted_result["content"])
    assert fitted_result["bytes_read"] == len(fitted_result["content"].encode("utf-8"))
    assert fitted_result["truncated"] is True
    assert fitted_result["sha256"] == read_result["sha256"]
    assert fitted_result["size"] == read_result["size"]


@pytest.mark.parametrize(
    "tool_calls_state",
    [
        pytest.param("missing", id="missing-tool-calls"),
        pytest.param("empty", id="empty-tool-calls"),
        pytest.param("large_file_write", id="large-file-write"),
    ],
)
def test_audit_summary_evidence_content_is_truncated_with_or_without_file_tool_calls(tool_calls_state):
    file_content = '读取文件："配置内容" \\n C:\\audit\\配置.json\r\n' * 3000
    short_content = '已保留的短证据："ok" \\n'
    shell_content = '命令输出："中文状态" \\n escaped=\\\\ and C:\\audit\\日志\n' * 3000
    write_content = '写入参数："新建配置" \\n body=\\payload\\\\\n' * 3000
    short_bytes_read = len(short_content.encode("utf-8"))
    file_bytes = file_content.encode("utf-8")
    shell_bytes = shell_content.encode("utf-8")
    payload = {
        "working_directory": "C:/audit",
        "server_confirmation_reasons": {},
        "audit_results": [
            {
                "tool_call_id": "risk-call",
                "score": 5,
                "reason": "需要确认",
                "deny": False,
                "file_checks": [],
            }
        ],
        "file_reads": [
            {
                "path": "配置/审计.json",
                "tool_call_id": "risk-call",
                "original_path": "配置/审计.json",
                "absolute_path": "C:/audit/配置/审计.json",
                "resolved_path": "C:/audit/配置/审计.json",
                "exists": True,
                "file_type": "regular_file",
                "size": len(file_bytes),
                "sha256": hashlib.sha256(file_bytes).hexdigest(),
                "status": "ok",
                "truncated": False,
                "content": file_content,
                "bytes_read": len(file_bytes),
                "remaining_file_reads": 1,
                "remaining_evidence_calls": 1,
                "remaining_shell_calls": 1,
            },
            {
                "path": "配置/历史.json",
                "tool_call_id": "prior-call",
                "original_path": "配置/历史.json",
                "absolute_path": "C:/audit/配置/历史.json",
                "resolved_path": "C:/audit/配置/历史.json",
                "exists": True,
                "file_type": "regular_file",
                "size": short_bytes_read + 100,
                "sha256": "b" * 64,
                "status": "ok",
                "truncated": True,
                "content": short_content,
                "bytes_read": short_bytes_read,
                "remaining_file_reads": 1,
                "remaining_evidence_calls": 1,
                "remaining_shell_calls": 1,
            },
        ],
        "shell_results": [
            {
                "tool_name": "execute_shell",
                "tool_call_id": "risk-call",
                "command": "Get-Content C:/audit/配置/审计.json",
                "status": "ok",
                "content": shell_content,
                "size": len(shell_bytes),
                "bytes_read": len(shell_bytes),
                "truncated": False,
                "remaining_file_reads": 1,
                "remaining_evidence_calls": 1,
                "remaining_shell_calls": 1,
            }
        ],
    }
    if tool_calls_state == "empty":
        payload["tool_calls"] = []
    elif tool_calls_state == "large_file_write":
        payload["tool_calls"] = [
            {
                "id": "write-call",
                "name": "file_tool",
                "arguments": {"operation": "write", "path": "配置/输出.txt", "content": write_content},
            }
        ]

    chat_params = {"context_window_k": 8, "max_tokens": 512}
    tools = get_audit_tool_schemas()
    system_prompt = "请依据审计数据总结待确认操作。"

    def request_context_tokens(candidate_payload):
        return estimate_request_context_tokens(
            [
                InternalMessage(role=MessageRole.SYSTEM, content=system_prompt),
                InternalMessage(role=MessageRole.USER, content=json.dumps(candidate_payload, ensure_ascii=False)),
            ],
            tools,
        )

    metadata_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    for evidence_field in ("file_reads", "shell_results"):
        for item in metadata_payload[evidence_field]:
            item["content"] = ""
    if tool_calls_state == "large_file_write":
        metadata_payload["tool_calls"][0]["arguments"]["content"] = ""

    input_limit = max_input_tokens(chat_params)
    original_payload_json = json.dumps(payload, ensure_ascii=False)
    assert request_context_tokens(metadata_payload) <= input_limit
    assert request_context_tokens(payload) > input_limit

    adapted_payload = fit_file_tool_payload_to_context(system_prompt, payload, chat_params)
    assert adapted_payload is not payload
    assert request_context_tokens(adapted_payload) <= input_limit
    assert json.dumps(payload, ensure_ascii=False) == original_payload_json

    for evidence_field in ("file_reads", "shell_results"):
        original = payload[evidence_field][0]
        adapted = adapted_payload[evidence_field][0]
        assert original["content"].startswith(adapted["content"])
        assert adapted["content"]
        assert adapted["content"] != original["content"]
        assert adapted["bytes_read"] == len(adapted["content"].encode("utf-8"))
        assert adapted["truncated"] is True
        assert {key: value for key, value in adapted.items() if key not in {"content", "bytes_read", "truncated"}} == {key: value for key, value in original.items() if key not in {"content", "bytes_read", "truncated"}}

    assert adapted_payload["file_reads"][1] == payload["file_reads"][1]
    if tool_calls_state == "large_file_write":
        adapted_write = adapted_payload["tool_calls"][0]
        adapted_write_content = adapted_write["arguments"]["content"]
        assert write_content.startswith(adapted_write_content)
        assert adapted_write_content != write_content
        assert adapted_write["argument_evidence"]["content"] == {
            "status": "ok",
            "size": len(write_content.encode("utf-8")),
            "sha256": hashlib.sha256(write_content.encode("utf-8")).hexdigest(),
            "truncated": True,
            "bytes_read": len(adapted_write_content.encode("utf-8")),
        }


def test_file_edit_and_patch_snapshots_record_mutation_targets_and_ignore_write_or_outside_paths(tmp_path):
    calls = [
        InternalToolCall(id="edit", name="file_tool", arguments={"operation": "edit", "path": "nested/new.txt", "old_text": "old", "new_text": "new"}),
        InternalToolCall(id="patch", name="file_tool", arguments={"operation": "patch", "path": "nested/patch.txt", "patch": "@@\n-old\n+new"}),
        InternalToolCall(id="write", name="file_tool", arguments={"operation": "write", "path": "overwrite.txt", "content": "new"}),
        InternalToolCall(id="outside", name="file_tool", arguments={"operation": "edit", "path": "../outside.txt", "old_text": "old", "new_text": "new"}),
    ]

    snapshots = _collect_file_mutation_snapshots(calls, tmp_path, [])

    snapshot = snapshots["edit"][0]
    assert snapshot["absolute_path"] == str((tmp_path / "nested" / "new.txt").resolve())
    assert snapshot["exists"] is False
    assert snapshot["status"] == "missing"
    assert snapshot["size"] is None
    assert snapshot["sha256"] is None
    assert snapshots["patch"][0]["absolute_path"] == str((tmp_path / "nested" / "patch.txt").resolve())
    assert "write" not in snapshots
    assert "outside" not in snapshots


def test_file_edit_snapshot_records_missing_target_before_workspace_exists(tmp_path):
    workspace = tmp_path / "temp_new_user"
    tool_call = InternalToolCall(id="edit", name="file_tool", arguments={"operation": "edit", "path": "nested/new.txt", "old_text": "old", "new_text": "new"})

    snapshots = _collect_file_mutation_snapshots([tool_call], workspace, [])

    snapshot = snapshots["edit"][0]
    assert workspace.exists() is False
    assert snapshot["absolute_path"] == str(workspace / "nested" / "new.txt")
    assert snapshot["exists"] is False
    assert snapshot["file_type"] == "missing"


def test_file_edit_snapshot_preserves_link_type_and_resolved_path(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    tool_call = InternalToolCall(id="call-1", name="file_tool", arguments={"operation": "edit", "path": "link.txt", "old_text": "content", "new_text": "new"})
    snapshots = _collect_file_mutation_snapshots([tool_call], tmp_path, [])

    snapshot = snapshots[tool_call.id][0]
    assert snapshot["absolute_path"] == str(link)
    assert snapshot["resolved_path"] == str(target.resolve())
    assert snapshot["exists"] is True
    assert snapshot["file_type"] == "symlink"
    assert snapshot["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("protect_system_directories", [True, False])
@pytest.mark.parametrize(
    "task_instruction",
    [
        None,
        "Create prepare.sh and verify.py; only write both scripts and do not run either script.",
    ],
)
async def test_auditor_can_read_arbitrary_file_and_sees_complete_round_context(tmp_path, monkeypatch, protect_system_directories, task_instruction):
    outside_file = tmp_path.parent / "audit-visible.txt"
    outside_file.write_text("server evidence", encoding="utf-8")
    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.protect_system_directories = protect_system_directories
    calls = []

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="read-1",
                            name="read_text_file",
                            arguments={"path": str(outside_file), "tool_call_id": "call-1"},
                        )
                    ],
                ),
                model="audit-model",
            )
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content='{"results":[{"tool_call_id":"call-1","score":0,"reason":"safe","deny":false,"file_checks":[]}]}'),
            model="audit-model",
        )

    monkeypatch.setattr("app.core.audit.service.channel_crud.get", get_channel)
    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)

    request_payload = {
        "confirmation_threshold": 5,
        "working_directory": str(tmp_path),
        "tool_calls": [
            {
                "tool_call_id": "call-1",
                "turn_index": 0,
                "tool_name": "execute_shell",
                "arguments": {"command": "python dynamic_target"},
            }
        ],
    }
    if task_instruction is not None:
        request_payload["task_instruction"] = task_instruction

    request_context, response_context = await _call_auditor(
        FakeDb(),
        cfg,
        request_payload,
        tmp_path,
    )

    assert len(calls) == 2
    expected_system_prompt = AUDIT_BATCH_PROMPT if protect_system_directories else AUDIT_STANDARD_BATCH_PROMPT
    if task_instruction is not None:
        expected_system_prompt += "\n\n" + AUDIT_TASK_AUTHORIZATION_PROMPT
    assert [call["messages"][0].content for call in calls] == [expected_system_prompt] * len(calls)
    assert calls[0]["tools"][0]["function"]["name"] == "read_text_file"
    assert [tool["function"]["name"] for tool in calls[1]["tools"]] == ["read_text_file", "execute_shell"]
    shell_tool = calls[1]["tools"][1]["function"]["parameters"]
    assert shell_tool["properties"]["execution_mode"]["enum"] == ["non_interactive"]
    assert "tool_call_id" in shell_tool["required"]
    assert all(call["max_tokens"] == DEFAULT_CHAT_MAX_TOKENS for call in calls)
    assert all(isinstance(call["request_context_tokens"], int) and call["request_context_tokens"] >= 0 for call in calls)
    user_payloads = [json.loads(call["messages"][1].content) for call in calls]
    assert all(payload["working_directory"] == str(tmp_path) for payload in user_payloads)
    assert all(payload["tool_calls"][0]["arguments"] == {"command": "python dynamic_target"} for payload in user_payloads)
    if task_instruction is None:
        assert all("task_instruction" not in payload for payload in user_payloads)
    else:
        assert all(payload["task_instruction"] == task_instruction for payload in user_payloads)
    expected_results = [{"tool_call_id": "call-1", "score": 0, "reason": "safe", "deny": False, "file_checks": []}]
    expected_response_context = {
        "content": {"results": expected_results},
        "file_reads": response_context["file_reads"],
        "shell_results": [],
        "parsed": {"results": expected_results},
    }
    assert {**response_context, "content": json.loads(response_context["content"])} == expected_response_context
    _, constructed_response_context = _auditor_response_context(expected_results, file_reads=response_context["file_reads"])
    assert {**constructed_response_context, "content": json.loads(constructed_response_context["content"])} == expected_response_context
    assert response_context["file_reads"][0]["content"] == "server evidence"
    assert response_context["file_reads"][0]["tool_call_id"] == "call-1"
    messages = request_context["messages"]
    assert isinstance(messages, list)
    assert [message["role"] for message in messages[:2]] == ["system", "user"]
    assert any(message["role"] == "tool" for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_case", ["invalid_id", "wrong_tool", "missing_id"])
@pytest.mark.parametrize(("audit_threshold", "expected_status"), [(5, "pending"), (0, "passed")])
async def test_audit_round_handles_unassociated_read_protocol_failure_by_threshold(monkeypatch, tmp_path, protocol_case, audit_threshold, expected_status):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.audit_threshold = audit_threshold
    captured = {}
    calls = []
    logs = []

    class FakeBoundLogger:
        def info(self, message):
            logs.append(message)

    class FakeLogger:
        def bind(self, **_kwargs):
            return FakeBoundLogger()

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if protocol_case == "wrong_tool":
                name = "unexpected_reader"
                arguments = {"path": "missing.txt", "tool_call_id": "call-1"}
            elif protocol_case == "missing_id":
                name = "read_text_file"
                arguments = {"path": "missing.txt"}
            else:
                name = "read_text_file"
                arguments = {"path": "missing.txt", "tool_call_id": "other-call"}
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="read-1", name=name, arguments=arguments)],
                ),
                model="audit-model",
            )
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content='{"results":[{"tool_call_id":"call-1","score":0,"reason":"safe","deny":false,"file_checks":[]},{"tool_call_id":"call-2","score":0,"reason":"safe","deny":false,"file_checks":[]}]}'),
            model="audit-model",
        )

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def summarize_pending(*_args, **_kwargs):
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "channel_crud", SimpleNamespace(get=get_channel))
    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)
    monkeypatch.setattr(service.LLMClient, "generate", generate)
    monkeypatch.setattr(service, "logger", FakeLogger())

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"}),
            InternalToolCall(id="call-2", name="execute_shell", arguments={"command": "echo also-ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result.status.value == expected_status
    assert all(detail["conclusion"] == expected_status for detail in captured["tool_details"])
    assert all(detail["score"] == 0 for detail in captured["tool_details"])
    assert all(detail["server_confirmation_reasons"][-1]["code"] == "file_read_protocol_invalid" for detail in captured["tool_details"])
    if expected_status == "pending":
        assert result.confirmation_payload["risk"] == audit_threshold
    expected_log_count = 3 if protocol_case == "invalid_id" else 2
    assert len(logs) == expected_log_count
    assert logs[0] == t("LOG_AUDIT_ROUND_STARTED", audit_record_id=123, model_id="audit-model", tool_count=2, source="web")
    assert "record_id=123" in logs[0]
    if protocol_case == "invalid_id":
        assert "tool=read_text_file" in logs[1]
        assert '"path": "missing.txt"' in logs[1]
    assert "record_id=123" in logs[-1]
    assert f"status={expected_status}" in logs[-1]
    assert "max_score=0" in logs[-1]
    assert f"summary={'Confirm command' if expected_status == 'pending' else '-'}" in logs[-1]
    if audit_threshold == 0:
        assert result.tool_results == ()
        assert result.confirmation_payload is None


@pytest.mark.asyncio
async def test_audit_file_reader_enforces_token_truncation_and_call_count(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha beta gamma delta", encoding="utf-8")
    second.write_text("one two three four", encoding="utf-8")
    budget = AuditReadBudget(limit=2)

    async def read(path):
        return await _execute_audit_read_tool_call(
            InternalToolCall(
                id="read-1",
                name="read_text_file",
                arguments={"path": str(path), "tool_call_id": "call-1"},
            ),
            expected_tool_call_ids={"call-1"},
            working_directory=tmp_path,
            read_budget=budget,
            max_tokens=1,
        )

    first_result = await read(first)
    second_result = await read(second)
    third_result = await read(first)

    assert first_result["status"] == "ok"
    assert second_result["status"] == "ok"
    assert "alpha beta gamma delta".startswith(first_result["content"])
    assert "one two three four".startswith(second_result["content"])
    assert estimate_tokens(first_result["content"]) <= 1
    assert estimate_tokens(second_result["content"]) <= 1
    assert first_result["bytes_read"] == len(first_result["content"].encode("utf-8"))
    assert second_result["bytes_read"] == len(second_result["content"].encode("utf-8"))
    assert first_result["truncated"] is True
    assert second_result["truncated"] is True
    assert third_result["status"] == "limit_exceeded"
    assert first_result["remaining_file_reads"] == 1
    assert second_result["remaining_file_reads"] == 0
    assert third_result["remaining_file_reads"] == 0
    assert budget.calls == 2
    assert budget.remaining == 0


@pytest.mark.parametrize("file_kind", ["missing", "truncated", "non_utf8"])
def test_read_failure_snapshots_are_rechecked_before_confirmed_execution(tmp_path, file_kind):
    import app.core.audit.service as service
    from app.core.session_reply_queue import executor_audit as executor_audit_module

    target = tmp_path / f"{file_kind}.txt"
    if file_kind == "truncated":
        target.write_bytes(b"0123456789")
    elif file_kind == "non_utf8":
        target.write_bytes(b"\xff\xfe")

    max_tokens = 1 if file_kind == "truncated" else 100
    read = service._read_for_audit_sync(str(target), "call-1", {"call-1"}, tmp_path, max_tokens)
    snapshots = service._file_snapshots_from_reads([read], {"call-1"})["call-1"]
    assert len(snapshots) == 1
    assert snapshots[0]["absolute_path"] == str(target)
    if file_kind == "missing":
        assert snapshots[0]["exists"] is False
        assert snapshots[0]["file_type"] == "missing"
    elif file_kind == "truncated":
        assert snapshots[0]["status"] == "ok"
        assert snapshots[0]["truncated"] is True
        assert snapshots[0]["sha256"]
    else:
        assert snapshots[0]["status"] == "unreadable"
        assert snapshots[0]["size"] == 2
        assert snapshots[0]["sha256"]

    details = [SimpleNamespace(file_snapshots=snapshots)]
    assert not executor_audit_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))
    if file_kind == "missing":
        target.write_text("created", encoding="utf-8")
    elif file_kind == "truncated":
        target.write_bytes(b"changed-data")
    else:
        target.write_bytes(b"\xfd\xfb")
    assert executor_audit_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))


@pytest.mark.parametrize("file_type", ["regular_file", "other"])
def test_unstable_read_failures_require_confirmation_without_persisted_snapshot(tmp_path, file_type):
    path = tmp_path / "unreadable"
    file_read = {
        "tool_call_id": "call-1",
        "original_path": str(path),
        "absolute_path": str(path),
        "resolved_path": str(path.resolve(strict=False)),
        "exists": True,
        "file_type": file_type,
        "status": "unreadable" if file_type == "regular_file" else "not_regular",
        "truncated": False,
        "error": "unavailable",
    }

    snapshots = _file_snapshots_from_reads([file_read], {"call-1"})

    assert snapshots == {}
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [file_read], [])


def test_file_patch_snapshot_converts_integrity_errors_to_conservative_snapshot(monkeypatch, tmp_path):
    import app.core.audit.service as service

    def fail_snapshot(*_args, **_kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(service, "create_file_integrity_snapshot", fail_snapshot)
    call = InternalToolCall(id="patch", name="file_tool", arguments={"operation": "patch", "path": "target.txt", "patch": "@@\n-old\n+new"})

    snapshots = service._collect_file_mutation_snapshots([call], tmp_path, [])

    snapshot = snapshots["patch"][0]
    assert snapshot["status"] == "unreadable"
    assert snapshot["file_type"] == "unknown"
    assert snapshot["exists"] is None
    assert "permission denied" in snapshot["error"]


@pytest.mark.asyncio
async def test_read_snapshot_is_bound_to_tool_detail_and_missing_check_requires_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    source = tmp_path / "evidence.txt"
    source.write_text("review me", encoding="utf-8")
    read = _read_for_audit_sync(str(source), "call-1", {"call-1", "call-2"}, tmp_path, 100)
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*args, **_kwargs):
        captured["request_payload"] = args[2]
        return _auditor_response_context(
            [
                {"tool_call_id": "call-1", "score": 0, "reason": "review me", "deny": False, "file_checks": []},
                {"tool_call_id": "call-2", "score": 0, "reason": "safe", "deny": False, "file_checks": []},
            ],
            file_reads=[read],
        )

    async def summarize_pending(*_args, **_kwargs):
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "python evidence.txt"}),
            InternalToolCall(id="call-2", name="execute_shell", arguments={"command": "echo ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result.status.value == "pending"
    first_detail, second_detail = captured["tool_details"]
    assert first_detail["file_snapshots"][0]["sha256"] == read["sha256"]
    assert first_detail["file_snapshots"][0]["content"] == read["content"]
    assert first_detail["file_snapshots"][0]["bytes_read"] == read["bytes_read"]
    assert first_detail["file_snapshots"][0]["tool_call_id"] == read["tool_call_id"]
    assert second_detail["file_snapshots"] == []
    assert first_detail["server_confirmation_reasons"][0]["code"] == "file_evidence_insufficient"
    assert first_detail["score"] == 0
    assert result.confirmation_payload["risk"] == cfg.security.audit_threshold
    assert "review me" in first_detail["reason"]
