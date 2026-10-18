import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.constants import ERR_AUDIT_ROUND_BLOCKED, ERR_AUDIT_SERVICE_FAILED_STOP, MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK
from app.core.crud.audit.audit import audit_crud
from app.core.crud.channel import cursor as channel_cursor_module
from app.core.dispatchers import ChatDispatcher
from app.core.i18n import t
from app.core.tools.shell import ShellExecutor
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request
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
    friendly_error = result_payloads[0]["reason"]
    assert isinstance(friendly_error, str)
    assert friendly_error
    assert all(payload["reason"] == friendly_error for payload in result_payloads)
    if rejection_kind == "local":
        assert "本地拦截" in friendly_error
    else:
        assert "上游" in friendly_error
        assert "413" in friendly_error

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
        assert record.error_reason == friendly_error
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
    assert all(payload["status"] == AuditRecordStatus.PENDING.value for payload in result_payloads)
    assert all(payload["reason"] == "requires confirmation" for payload in result_payloads)
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
            assert tool_payload["status"] == expected_status.value
            assert tool_payload["error"] == expected_error
            assert isinstance(tool_payload["reason"], str)
            assert tool_payload["reason"]
            if policy_denied:
                assert tool_payload["reason"] == policy_denial_reason
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
        assert tool_result_payload["status"] == expected_status.value
        assert tool_result_payload["error"] == expected_error
        assert PRIVATE_RESPONSE not in json.dumps(tool_result_payload, ensure_ascii=False)
        if policy_denied:
            assert record.error_reason is None
            assert tool_result_payload["reason"] == policy_denial_reason
        else:
            assert record.error_reason == tool_result_payload["reason"]
        assert details[0].reason == tool_result_payload["reason"]

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
