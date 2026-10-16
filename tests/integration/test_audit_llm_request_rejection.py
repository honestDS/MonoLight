import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.constants import (
    ERR_AUDIT_SERVICE_FAILED_STOP,
    ERR_LLM_REQUEST_TOO_LARGE,
    ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE,
)
from app.core.crud.audit.audit import audit_crud
from app.core.exceptions import LLMRequestRejectedException
from app.core.i18n import t
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditFailureType,
    AuditRecordStatus,
    AuditToolConclusion,
)
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig

AUDIT_MODEL_ID = "audit-model"
UID = "audit-user"
SESSION_ID = "audit-session"
PRIVATE_RESPONSE = "<html>private</html>"


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
    expected_score: int | None,
) -> None:
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert isinstance(result.request_error, LLMRequestRejectedException)

    request_error = result.request_error
    friendly_error = request_error.render_message()
    assert friendly_error
    assert friendly_error != request_error.message
    assert PRIVATE_RESPONSE not in json.dumps(request_error.to_response(), ensure_ascii=False)
    assert request_error.source == rejection_kind
    assert request_error.reason == "request_too_large"
    if rejection_kind == "local":
        assert request_error.message == ERR_LLM_REQUEST_TOO_LARGE
        assert "本地拦截" in friendly_error
        assert request_error.allow_channel_fallback is False
    else:
        assert request_error.message == ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE
        assert request_error.upstream_status == 413
        assert "上游" in friendly_error
        assert "413" in friendly_error
        assert request_error.allow_channel_fallback is True

    result_payloads = [json.loads(message.content or "{}") for message in result.tool_results]
    assert result_payloads
    assert all(payload["status"] == AuditRecordStatus.AUDIT_FAILED.value for payload in result_payloads)
    assert all(payload["error"] == t(ERR_AUDIT_SERVICE_FAILED_STOP) for payload in result_payloads)
    assert all(payload["reason"] == friendly_error for payload in result_payloads)
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
        assert details[0].score == expected_score
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
    await _assert_failed_round(audit_database, result, audit_root=tmp_path, rejection_kind=rejection_kind, expected_score=None)


@pytest.mark.parametrize("rejection_kind", ["local", "upstream"], ids=["local", "upstream-413"])
@pytest.mark.asyncio
async def test_summary_request_rejection_converts_pending_audit_to_failed_without_confirmation(
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
    await _assert_failed_round(audit_database, result, audit_root=tmp_path, rejection_kind=rejection_kind, expected_score=5)

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        assert record is not None
        assert record.intent_summary is None
