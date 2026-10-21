import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.constants import (
    ERR_AUDIT_ROUND_BLOCKED,
    ERR_AUDIT_SERVICE_FAILED_STOP,
    ERR_TOOL_SHELL_BLACKLISTED,
    MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK,
    MSG_AUDIT_WAITING_CONFIRMATION,
)
from app.core.context import ContextManager
from app.core.crud.audit.audit import audit_crud
from app.core.crud.channel import cursor as channel_cursor_module
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.tools.shell import ShellExecutor
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
)
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.user import User
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

AUDIT_MODEL_ID = "audit-model"
CHAT_MODEL_ID = "chat-model"
UID = "audit-user"
SESSION_ID = "audit-session"
PRIVATE_RESPONSE = "<html>private</html>"
CHAT_PRIMARY_URL = "https://chat-primary.invalid"
CHAT_FALLBACK_URL = "https://chat-fallback.invalid"
FINAL_CHAT_CONTENT = "审计未通过，未执行命令。"


@pytest.fixture
async def audit_database(database_factory):
    async with database_factory(name="audit-llm-request-rejection.db") as session_factory:
        yield session_factory


def _audit_config(tmp_path: Path) -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": 1,
                            "model_id": AUDIT_MODEL_ID,
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                }
            },
            "security": {
                "audit_channel_id": 1,
                "audit_model_id": AUDIT_MODEL_ID,
                "audit_threshold": 3,
                "audit_report_language": "zh",
            },
            "tool": {
                "enabled_tools": ["execute_shell"],
                "allowed_operation_dirs": [str(tmp_path)],
            },
            "other": {},
            "memory": {"enabled": False},
        }
    )


def _audit_channel() -> ModelChannel:
    model_fields = {
        "context_window_k": 32,
        "max_tokens": 512,
        "temperature": 0,
        "top_p": 1,
    }
    return ModelChannel(
        id=1,
        name="audit-request-rejection-channel",
        api_key="enc:v1:test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {
                "model_id": AUDIT_MODEL_ID,
                "usage": "CHAT",
                "protocol": "OPENAI",
                **model_fields,
            }
        ],
    )


async def _seed_audit_channel(session_factory) -> None:
    async with session_factory() as db:
        db.add(_audit_channel())
        await db.commit()


def _patch_audit_root(monkeypatch: pytest.MonkeyPatch, audit_root: Path) -> None:
    async def persist_in_test_root(db, **kwargs: Any) -> bool:
        return await persist_prepared_audit_round(db, audit_root=audit_root, **kwargs)

    monkeypatch.setattr(audit_service_module, "persist_prepared_audit_round", persist_in_test_root)


def _raise_request_rejection(kind: str, kwargs: dict[str, Any]) -> None:
    if kind == "local":
        payload = {
            "model": kwargs["model_id"],
            "messages": [message.model_dump(mode="json") for message in kwargs["messages"]],
        }
        serialize_llm_json_request(payload, max_size_bytes=0)
        raise AssertionError("serialize_llm_json_request should have rejected the request")

    rejection = build_upstream_rejection(status=413, detail=PRIVATE_RESPONSE)
    assert rejection is not None
    raise rejection


def _pending_audit_response() -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=json.dumps(
                {
                    "results": [
                        {
                            "tool_call_id": "shell-call",
                            "score": 5,
                            "deny": False,
                            "reason": "requires confirmation",
                            "file_checks": [],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ),
        model=AUDIT_MODEL_ID,
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        finish_reason="stop",
    )


async def _assert_failed_round(
    session_factory,
    result,
    *,
    audit_root: Path,
    rejection_kind: str,
) -> None:
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert result.confirmation_payload is None

    result_payloads = [json.loads(message.content or "{}") for message in result.tool_results]
    assert result_payloads
    assert all(set(payload) == {"status", "error"} for payload in result_payloads)
    assert all(payload["status"] == AuditRecordStatus.AUDIT_FAILED.value for payload in result_payloads)
    assert all(payload["error"] == t(ERR_AUDIT_SERVICE_FAILED_STOP) for payload in result_payloads)
    assert PRIVATE_RESPONSE not in json.dumps(result_payloads, ensure_ascii=False)

    async with session_factory() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())

        assert record is not None
        assert record.status is AuditRecordStatus.AUDIT_FAILED
        assert record.failure_type is AuditFailureType.AUDIT_SERVICE_FAILED
        friendly_error = record.error_reason
        assert isinstance(friendly_error, str)
        assert friendly_error
        if rejection_kind == "local":
            assert "本地拦截" in friendly_error
        else:
            assert "上游" in friendly_error
            assert "413" in friendly_error
        assert record.execution_claim_token is None
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.AUDIT_FAILED
        assert details[0].score is None
        assert details[0].reason == friendly_error
        assert claims == []
        assert executions == []
        assert await audit_crud.get_current_confirmation(db, uid=UID, session_id=SESSION_ID) is None
        assert await audit_crud.get_confirmation_claim(db, uid=UID, session_id=SESSION_ID) is None

        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        assert context_path.parent == audit_root / f"temp_{UID}"
        assert PRIVATE_RESPONSE not in context_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("rejection_kind", ["local", "upstream"], ids=["local", "upstream-413"])
@pytest.mark.asyncio
async def test_scoring_request_rejection_persists_failed_audit_without_execution(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejection_kind: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path)

    calls = []

    async def generate(**kwargs: Any) -> None:
        calls.append(kwargs)
        _raise_request_rejection(rejection_kind, kwargs)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    tool_call = InternalToolCall(
        id="shell-call",
        name="execute_shell",
        arguments={"command": "echo audit-safe"},
    )

    async with audit_database() as db:
        result = await audit_service_module.audit_tool_round(
            db,
            cfg=_audit_config(tmp_path),
            tool_calls=[tool_call],
            source_assistant_message_id=101,
            uid=UID,
            operator_username="tester",
            session_id=SESSION_ID,
            source="integration",
            language="zh",
            working_directory=tmp_path,
        )

    assert result is not None
    assert len(calls) == 1
    assert all(call["model_id"] == AUDIT_MODEL_ID for call in calls)
    assert all(call["base_url"] == "https://llm.invalid" for call in calls)
    await _assert_failed_round(audit_database, result, audit_root=tmp_path, rejection_kind=rejection_kind)


@pytest.mark.parametrize("rejection_kind", ["local", "upstream"], ids=["local", "upstream-413"])
@pytest.mark.asyncio
async def test_summary_request_rejection_preserves_pending_confirmation_with_fallback(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejection_kind: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path)

    calls = []

    async def generate(**kwargs: Any) -> InternalResponse | None:
        calls.append(kwargs)
        if len(calls) == 1:
            return _pending_audit_response()
        _raise_request_rejection(rejection_kind, kwargs)
        return None

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    tool_call = InternalToolCall(
        id="shell-call",
        name="execute_shell",
        arguments={"command": "echo audit-safe"},
    )

    async with audit_database() as db:
        result = await audit_service_module.audit_tool_round(
            db,
            cfg=_audit_config(tmp_path),
            tool_calls=[tool_call],
            source_assistant_message_id=102,
            uid=UID,
            operator_username="tester",
            session_id=SESSION_ID,
            source="integration",
            language="zh",
            working_directory=tmp_path,
        )

    assert result is not None
    assert len(calls) == 2
    assert all(call["model_id"] == AUDIT_MODEL_ID for call in calls)
    assert all(call["base_url"] == "https://llm.invalid" for call in calls)

    expected_summary = t(MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK, locale="zh")
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["summary"] == expected_summary
    assert result.confirmation_payload["risk"] == 5
    assert result.confirmation_payload["status"] == AuditRecordStatus.PENDING.value

    result_payloads = [json.loads(message.content or "{}") for message in result.tool_results]
    assert result_payloads
    assert all(set(payload) == {"status", "error"} for payload in result_payloads)
    assert all(payload["status"] == AuditRecordStatus.PENDING.value for payload in result_payloads)
    assert all(payload["error"] == t(MSG_AUDIT_WAITING_CONFIRMATION) for payload in result_payloads)
    assert PRIVATE_RESPONSE not in json.dumps(result.confirmation_payload, ensure_ascii=False)
    assert PRIVATE_RESPONSE not in json.dumps(result_payloads, ensure_ascii=False)

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())

        assert record is not None
        assert record.status is AuditRecordStatus.PENDING
        assert record.failure_type is None
        assert record.error_reason is None
        assert record.intent_summary == expected_summary
        assert record.expires_at is not None
        assert record.execution_claim_token is None
        assert len(details) == 1
        assert details[0].score == 5
        assert details[0].conclusion is AuditToolConclusion.PENDING
        assert details[0].reason == "requires confirmation"
        assert claims == []
        assert executions == []

        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        assert context_path.parent == tmp_path / f"temp_{UID}"
        context_text = context_path.read_text(encoding="utf-8")
        assert PRIVATE_RESPONSE not in context_text
        context_payload = json.loads(context_text)
        summary_context = context_payload["summary"]
        assert summary_context["fallback"] is True
        summary_error = summary_context["error"]
        assert isinstance(summary_error, str)
        assert summary_error
        if rejection_kind == "local":
            assert "本地拦截" in summary_error
        else:
            assert "上游" in summary_error
            assert "413" in summary_error


@pytest.mark.parametrize(
    ("rejection_kind", "stream_callback"),
    [
        ("local", False),
        ("local", True),
        ("upstream", False),
        ("upstream", True),
        ("policy-denied", False),
        ("policy-denied", True),
    ],
    ids=[
        "local-non-stream",
        "local-stream",
        "upstream-413-non-stream",
        "upstream-413-stream",
        "policy-denied-non-stream",
        "policy-denied-stream",
    ],
)
@pytest.mark.asyncio
async def test_interactive_request_rejection_returns_failed_tool_result_to_chat(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejection_kind: str,
    stream_callback: bool,
) -> None:
    policy_denied = rejection_kind == "policy-denied"
    policy_denial_reason = "操作将修改系统受保护目录，已拒绝执行。"
    expected_status = AuditRecordStatus.BLOCKED if policy_denied else AuditRecordStatus.AUDIT_FAILED
    expected_error = t(ERR_AUDIT_ROUND_BLOCKED if policy_denied else ERR_AUDIT_SERVICE_FAILED_STOP)
    expected_failure_type = None if policy_denied else AuditFailureType.AUDIT_SERVICE_FAILED
    expected_conclusion = AuditToolConclusion.BLOCKED if policy_denied else AuditToolConclusion.AUDIT_FAILED
    expected_score = 0 if policy_denied else None

    audit_cfg = _audit_config(tmp_path)
    profile_configs = audit_cfg.model_dump()
    profile_configs["channel"]["chat_channel"]["rules"] = [
        {"channel_id": 2, "model_id": CHAT_MODEL_ID, "priority": 1, "weight": 1},
        {"channel_id": 3, "model_id": CHAT_MODEL_ID, "priority": 2, "weight": 1},
    ]
    chat_model = dict(_audit_channel().model_ids[0])
    chat_model["model_id"] = CHAT_MODEL_ID

    async with audit_database() as db:
        db.add_all(
            [
                _audit_channel(),
                ModelChannel(
                    id=2,
                    name="chat-primary-channel",
                    api_key="enc:v1:test-key",
                    base_url=CHAT_PRIMARY_URL,
                    model_ids=[dict(chat_model)],
                ),
                ModelChannel(
                    id=3,
                    name="chat-fallback-channel",
                    api_key="enc:v1:test-key",
                    base_url=CHAT_FALLBACK_URL,
                    model_ids=[dict(chat_model)],
                ),
                User(uid=UID, username="audit-request-rejection-user"),
            ]
        )
        profile = Profile(
            uid=UID,
            name="audit-request-rejection-profile",
            configs=profile_configs,
            is_default=True,
        )
        db.add(profile)
        await db.flush()
        assert profile.id is not None
        db.add(
            ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                goal_mode=False,
                max_turns=5,
            )
        )
        await db.commit()
        profile_id = profile.id

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", audit_database)
    _patch_audit_root(monkeypatch, tmp_path)

    shell_execution_count = 0

    async def count_shell_execution(*_args: Any, **_kwargs: Any) -> str:
        nonlocal shell_execution_count
        shell_execution_count += 1
        return "unexpected shell execution"

    monkeypatch.setattr(ShellExecutor, "execute", count_shell_execution)

    calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        calls.append(
            {
                "model_id": kwargs["model_id"],
                "base_url": kwargs["base_url"],
                "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
            }
        )
        if kwargs["model_id"] == AUDIT_MODEL_ID:
            if policy_denied:
                return InternalResponse(
                    message=InternalMessage(
                        role=MessageRole.ASSISTANT,
                        content=json.dumps(
                            {
                                "results": [
                                    {
                                        "tool_call_id": "shell-call",
                                        "score": 0,
                                        "deny": True,
                                        "reason": policy_denial_reason,
                                        "file_checks": [],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    model=AUDIT_MODEL_ID,
                    usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                    finish_reason="stop",
                )
            _raise_request_rejection(rejection_kind, kwargs)

        assert kwargs["model_id"] == CHAT_MODEL_ID
        chat_call_count = sum(call["model_id"] == CHAT_MODEL_ID for call in calls)
        if chat_call_count == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="shell-call",
                            name="execute_shell",
                            arguments={
                                "command": (f'echo audit-safe > "{tmp_path / "data" / "skills" / "protected.txt"}"' if policy_denied else "echo audit-safe"),
                                "execution_mode": "non_interactive",
                            },
                        )
                    ],
                ),
                model=CHAT_MODEL_ID,
                usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                finish_reason="tool_calls",
            )
        if chat_call_count == 2:
            tool_messages = [message for message in kwargs["messages"] if message.role == MessageRole.TOOL and message.tool_call_id == "shell-call"]
            assert len(tool_messages) == 1
            tool_payload = json.loads(tool_messages[0].content or "{}")
            assert set(tool_payload) == {"status", "error"}
            assert tool_payload["status"] == expected_status.value
            assert tool_payload["error"] == expected_error
            assert PRIVATE_RESPONSE not in json.dumps(tool_payload, ensure_ascii=False)
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content=FINAL_CHAT_CONTENT,
                ),
                model=CHAT_MODEL_ID,
                usage={"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
                finish_reason="stop",
            )
        raise AssertionError("unexpected third chat model call")

    async def generate_with_stream_callback(*, on_content, on_reasoning=None, **kwargs: Any) -> InternalResponse:
        del on_reasoning
        response = await generate(**kwargs)
        if response.message.content:
            await on_content(response.message.content)
        return response

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    monkeypatch.setattr(audit_service_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)

    async def no_additional_user_messages():
        return None

    async def collect_event(event: dict[str, Any]) -> None:
        events.append(event)

    async with audit_database() as db:
        response = await ChatDispatcher.dispatch(
            db,
            message="请执行安全命令",
            uid=UID,
            session_id=SESSION_ID,
            persisted_profile_id=profile_id,
            additional_user_messages_fetcher=no_additional_user_messages,
            stream_event_callback=collect_event if stream_callback else None,
        )

    assert response["choices"][0]["message"]["content"] == FINAL_CHAT_CONTENT
    assert shell_execution_count == 0
    assert [call["model_id"] for call in calls] == [CHAT_MODEL_ID, AUDIT_MODEL_ID, CHAT_MODEL_ID]
    assert [call["base_url"] for call in calls] == [CHAT_PRIMARY_URL, "https://llm.invalid", CHAT_PRIMARY_URL]
    assert all(call["base_url"] != CHAT_FALLBACK_URL for call in calls)
    if stream_callback:
        assert not any(event.get("type") == "error" for event in events)
        assert any(event.get("type") == "content" and event.get("content") == FINAL_CHAT_CONTENT for event in events)

    async with audit_database() as db:
        records = list((await db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID))).scalars().all())
        assert len(records) == 1
        record = records[0]
        assert record.status is expected_status
        assert record.failure_type is expected_failure_type

        details = await audit_crud.list_tool_details(db, record.id)
        assert len(details) == 1
        assert details[0].conclusion is expected_conclusion
        assert details[0].score == expected_score

        stored_messages = list((await db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())
        tool_results = [message for message in stored_messages if message.type == MessageType.TOOL_RESULT]
        assert len(tool_results) == 1
        stored_tool_result = InternalMessage.model_validate_json(tool_results[0].content or "{}")
        tool_result_payload = json.loads(stored_tool_result.content or "{}")
        assert stored_tool_result.tool_call_id == "shell-call"
        assert set(tool_result_payload) == {"status", "error"}
        assert tool_result_payload["status"] == expected_status.value
        assert tool_result_payload["error"] == expected_error
        assert PRIVATE_RESPONSE not in json.dumps(tool_result_payload, ensure_ascii=False)
        if policy_denied:
            assert record.error_reason is None
            assert details[0].reason == policy_denial_reason
        else:
            assert isinstance(record.error_reason, str)
            assert record.error_reason
            assert details[0].reason == record.error_reason

        tool_calls = [message for message in stored_messages if message.type == MessageType.TOOL_CALL]
        assert len(tool_calls) == 1
        tool_call_payload = json.loads(tool_calls[0].content or "{}")
        assert [item["id"] for item in tool_call_payload["tool_calls"]] == ["shell-call"]
        assistant_texts = [message.content for message in stored_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
        assert assistant_texts[-1] == FINAL_CHAT_CONTENT
        assert not any(message.type == MessageType.AUDIT_CONFIRMATION for message in stored_messages)
        assert not any(message.role == MessageRole.ERR for message in stored_messages)

        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == record.id))).scalars().all())
        assert claims == []
        assert executions == []
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        assert context_path.parent == tmp_path / f"temp_{UID}"
        assert PRIVATE_RESPONSE not in context_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("blacklisted_command", ["powershell", "future-denied-command"])
@pytest.mark.parametrize(
    "transformer_cls",
    [OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer],
    ids=["openai-chat-completions", "openai-responses"],
)
@pytest.mark.asyncio
async def test_shell_blacklist_feedback_survives_history_reload_and_locale_switch(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blacklisted_command: str,
    transformer_cls,
) -> None:
    locale_token = set_current_locale("zh")
    try:
        await _seed_audit_channel(audit_database)
        _patch_audit_root(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ShellExecutor,
            "COMMAND_BLACKLIST",
            [*ShellExecutor.COMMAND_BLACKLIST, blacklisted_command],
        )

        private_argument_marker = "PRIVATE_ARGUMENT_MARKER"
        shell_call = InternalToolCall(
            id="shell-call",
            name="execute_shell",
            arguments={
                "command": f"{blacklisted_command.upper()} {private_argument_marker}",
                "execution_mode": "non_interactive",
            },
        )
        background_call = InternalToolCall(
            id="background-call",
            name="manage_background_tasks",
            arguments={"operation": "list"},
        )

        llm_calls: list[dict[str, Any]] = []
        execution_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def count_generate(**kwargs: Any) -> None:
            llm_calls.append(kwargs)
            raise AssertionError("a system blacklist rejection must not call the audit model")

        async def count_shell_execution(*args: Any, **kwargs: Any) -> str:
            execution_calls.append((args, kwargs))
            return "unexpected shell execution"

        monkeypatch.setattr(audit_service_module.LLMClient, "generate", count_generate)
        monkeypatch.setattr(ShellExecutor, "execute", count_shell_execution)

        async with audit_database() as db:
            result = await audit_service_module.audit_tool_round(
                db,
                cfg=_audit_config(tmp_path),
                tool_calls=[shell_call, background_call],
                source_assistant_message_id=103,
                uid=UID,
                operator_username="tester",
                session_id=SESSION_ID,
                source="integration",
                language="zh",
                working_directory=tmp_path,
            )

        assert result is not None
        assert result.status is AuditRecordStatus.BLOCKED
        assert result.may_execute is False
        assert result.confirmation_payload is None
        assert llm_calls == []
        assert execution_calls == []

        expected_zh_error = t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted_command)
        expected_zh_generic_error = t(ERR_AUDIT_ROUND_BLOCKED)
        assert [message.tool_call_id for message in result.tool_results] == ["shell-call", "background-call"]
        shell_feedback = json.loads(result.tool_results[0].content or "{}")
        background_feedback = json.loads(result.tool_results[1].content or "{}")
        assert shell_feedback == {
            "status": AuditRecordStatus.BLOCKED.value,
            "error": expected_zh_error,
            "reason": expected_zh_error,
        }
        assert background_feedback == {
            "status": AuditRecordStatus.BLOCKED.value,
            "error": expected_zh_generic_error,
        }
        assert private_argument_marker not in json.dumps(shell_feedback, ensure_ascii=False)

        async with audit_database() as db:
            record = await audit_crud.get_record(db, result.audit_record_id)
            details = await audit_crud.list_tool_details(db, result.audit_record_id)
            claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
            executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())

            assert record is not None
            assert record.status is AuditRecordStatus.BLOCKED
            assert record.failure_type is None
            assert record.error_reason is None
            assert len(details) == 2
            shell_detail = next(detail for detail in details if detail.original_tool_call_id == shell_call.id)
            background_detail = next(detail for detail in details if detail.original_tool_call_id == background_call.id)
            assert shell_detail.conclusion is AuditToolConclusion.BLOCKED
            assert shell_detail.score == 10
            assert shell_detail.reason == expected_zh_error
            assert background_detail.conclusion is AuditToolConclusion.PASSED
            assert background_detail.score == 0
            assert claims == []
            assert executions == []
            assert await audit_crud.get_current_confirmation(db, uid=UID, session_id=SESSION_ID) is None

        fake_command = "echo audit-safe"
        fake_shell_call = InternalToolCall(
            id="fake-shell-call",
            name="execute_shell",
            arguments={"command": fake_command},
        )
        actual_tool_result = result.tool_results[0].model_copy(
            update={
                "content": json.dumps(
                    {
                        **shell_feedback,
                        "score": 10,
                        "file_checks": [{"path": private_argument_marker}],
                        "reason_marker": private_argument_marker,
                    },
                    ensure_ascii=False,
                )
            }
        )
        fake_tool_result = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=fake_shell_call.id,
            content=json.dumps(
                {
                    "status": AuditRecordStatus.BLOCKED.value,
                    "error": t(ERR_TOOL_SHELL_BLACKLISTED, command="not-a-blacklisted-command"),
                    "reason": private_argument_marker,
                    "score": 10,
                    "file_checks": [{"path": private_argument_marker}],
                    "reason_marker": private_argument_marker,
                },
                ensure_ascii=False,
            ),
        )
        history_tool_calls = InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[shell_call, background_call, fake_shell_call],
        )
        history_tool_results = [actual_tool_result, result.tool_results[1], fake_tool_result]

        async with audit_database() as db:
            db.add(User(uid=UID, username="audit-shell-blacklist-user"))
            profile = Profile(
                uid=UID,
                name="audit-shell-blacklist-profile",
                configs={},
                is_default=True,
            )
            db.add(profile)
            await db.flush()
            assert profile.id is not None
            db.add(
                ChatSession(
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=profile.id,
                    goal_mode=False,
                    max_turns=5,
                )
            )
            await db.flush()

            stored_messages = [
                Message(
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=profile.id,
                    role=MessageRole.ASSISTANT,
                    type=MessageType.TOOL_CALL,
                    content=history_tool_calls.model_dump_json(),
                    is_processed=True,
                ),
                *[
                    Message(
                        uid=UID,
                        session_id=SESSION_ID,
                        profile_id=profile.id,
                        role=MessageRole.TOOL,
                        type=MessageType.TOOL_RESULT,
                        content=message.model_dump_json(),
                        is_processed=True,
                    )
                    for message in history_tool_results
                ],
            ]
            db.add_all(stored_messages)
            await db.flush()
            primary_result_message_id = stored_messages[1].id
            stored_primary_content = stored_messages[1].content
            await db.commit()

        english_locale_token = set_current_locale("en")
        try:
            expected_en_error = t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted_command)
            expected_en_generic_error = t(ERR_AUDIT_ROUND_BLOCKED)
            async with audit_database() as db:
                history_messages = list((await db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())

            parsed_history = parse_db_messages_to_internal(history_messages)
            prepared_history = ContextManager.prepare_messages_for_model_request(
                messages=parsed_history,
                context_window_k=32,
                max_tokens=512,
            )
            prepared_results = {message.tool_call_id: json.loads(message.content or "{}") for message in prepared_history if message.role == MessageRole.TOOL}
            expected_en_shell_feedback = {
                "status": AuditRecordStatus.BLOCKED.value,
                "error": expected_en_error,
                "reason": expected_en_error,
            }
            expected_en_generic_feedback = {
                "status": AuditRecordStatus.BLOCKED.value,
                "error": expected_en_generic_error,
            }
            assert prepared_results["shell-call"] == expected_en_shell_feedback
            assert prepared_results["background-call"] == expected_en_generic_feedback
            assert prepared_results["fake-shell-call"] == expected_en_generic_feedback

            provider_messages = transformer_cls.to_provider(prepared_history)
            if transformer_cls is OpenAIChatCompletionsTransformer:
                provider_shell_result = next(item for item in provider_messages if item.get("role") == "tool" and item.get("tool_call_id") == "shell-call")
                provider_background_result = next(item for item in provider_messages if item.get("role") == "tool" and item.get("tool_call_id") == "background-call")
                provider_fake_result = next(item for item in provider_messages if item.get("role") == "tool" and item.get("tool_call_id") == "fake-shell-call")
                provider_payloads = [
                    json.loads(provider_shell_result["content"]),
                    json.loads(provider_background_result["content"]),
                    json.loads(provider_fake_result["content"]),
                ]
                shell_result_content = provider_shell_result["content"]
            else:
                provider_shell_result = next(item for item in provider_messages if item.get("type") == "function_call_output" and item.get("call_id") == "shell-call")
                provider_background_result = next(item for item in provider_messages if item.get("type") == "function_call_output" and item.get("call_id") == "background-call")
                provider_fake_result = next(item for item in provider_messages if item.get("type") == "function_call_output" and item.get("call_id") == "fake-shell-call")
                provider_payloads = [
                    json.loads(provider_shell_result["output"]),
                    json.loads(provider_background_result["output"]),
                    json.loads(provider_fake_result["output"]),
                ]
                shell_result_content = provider_shell_result["output"]

            assert provider_payloads == [
                expected_en_shell_feedback,
                expected_en_generic_feedback,
                expected_en_generic_feedback,
            ]
            assert blacklisted_command in shell_result_content
            assert private_argument_marker not in shell_result_content
            assert "reason_marker" not in shell_result_content
            assert "file_checks" not in shell_result_content
            assert "score" not in shell_result_content

            async with audit_database() as db:
                persisted_primary = await db.get(Message, primary_result_message_id)
                assert persisted_primary is not None
                assert persisted_primary.content == stored_primary_content
        finally:
            reset_current_locale(english_locale_token)
    finally:
        reset_current_locale(locale_token)
