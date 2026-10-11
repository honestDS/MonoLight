import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.confirmation_events as confirmation_events_module
import app.core.audit.service as audit_service_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.security as security_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.providers.database as database_provider
from app.api.v1 import chat as chat_api
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_notifier import SessionNotifier
from app.models.audit import AuditExecutionRecord, AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from app.providers.database import get_db
from tests.integration.test_chat_websocket_permissions import _Socket

SESSION_ID = "actor-session"
OWNER_UID = "owner"
ADMIN_UID = "admin"
PROFILE_ID = 1


def _profile_configs() -> dict[str, Any]:
    return {
        "channel": {
            "chat_channel": {
                "rules": [
                    {
                        "channel_id": 1,
                        "model_id": "chat-model",
                        "priority": 1,
                        "weight": 1,
                    }
                ]
            }
        },
        "security": {
            "audit_channel_id": 1,
            "audit_model_id": "audit-model",
            "audit_threshold": 5,
        },
        "tool": {"enabled_tools": ["execute_shell"]},
        "other": {},
        "memory": {"enabled": False, "precheck_enabled": False},
    }


def _model_channel() -> ModelChannel:
    model_fields = {
        "context_window_k": 32,
        "max_tokens": 512,
        "temperature": 0,
        "top_p": 1,
    }
    return ModelChannel(
        id=1,
        name="actor-audit-channel",
        api_key="enc:v1:stored-test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {"model_id": "chat-model", "usage": "CHAT", "protocol": "OPENAI", **model_fields},
            {"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI", **model_fields},
        ],
    )


async def _seed_actor_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    source: str,
) -> None:
    async with factory() as db:
        db.add_all(
            [
                User(uid=OWNER_UID, username="alice"),
                User(uid=ADMIN_UID, username="admin_before", is_superuser=True),
                Profile(id=PROFILE_ID, uid=OWNER_UID, name="owner-profile", configs=_profile_configs()),
                ChatSession(
                    session_id=SESSION_ID,
                    uid=OWNER_UID,
                    profile_id=PROFILE_ID,
                    profile_override_id=PROFILE_ID,
                    source=source,
                    reply_target_source=source,
                    max_turns=5,
                    goal_mode=False,
                ),
                _model_channel(),
            ]
        )
        await db.commit()


@pytest_asyncio.fixture
async def actor_workflow_runtime(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], Path, SessionNotifier]]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="chat-audit-actor-workflow.sqlite3",
    ) as factory:
        monkeypatch.setattr(database_provider, "AsyncSessionLocal", factory)
        monkeypatch.setattr(chat_api, "AsyncSessionLocal", factory)
        monkeypatch.setattr(security_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")

        async def runtime_settings(_db: AsyncSession) -> SimpleNamespace:
            return SimpleNamespace(log_locale="zh")

        monkeypatch.setattr(chat_api.system_setting_crud, "get_runtime_settings", runtime_settings)

        async def noop_transport(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        monkeypatch.setattr(executor_interactive_module, "send_session_stream_event", noop_transport)
        monkeypatch.setattr(confirmation_events_module, "send_session_event", noop_transport)

        notifier = SessionNotifier()
        monkeypatch.setattr(chat_api, "session_notifier", notifier)
        audit_root = tmp_path / "audit"

        async def fake_call_auditor(
            _db: AsyncSession,
            _cfg: Any,
            request_payload: dict[str, Any],
            _working_directory: Path,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            tool_call_id = request_payload["tool_calls"][0]["tool_call_id"]
            return (
                {"messages": []},
                {
                    "parsed": {
                        "results": [
                            {
                                "tool_call_id": tool_call_id,
                                "score": 6,
                                "reason": "test confirmation",
                                "file_checks": [],
                            }
                        ]
                    },
                    "file_reads": [],
                },
            )

        monkeypatch.setattr(audit_service_module, "_call_auditor", fake_call_auditor)
        original_persist = audit_service_module.persist_prepared_audit_round

        async def persist_in_test_root(db: AsyncSession, **kwargs: Any) -> bool:
            return await original_persist(db, audit_root=audit_root, **kwargs)

        monkeypatch.setattr(audit_service_module, "persist_prepared_audit_round", persist_in_test_root)

        async def fake_generate(**kwargs: Any) -> InternalResponse:
            model_id = kwargs["model_id"]
            if model_id == "audit-model":
                return InternalResponse(
                    message=InternalMessage(
                        role=MessageRole.ASSISTANT,
                        content="计划执行 echo actor；确认后才会执行，当前等待确认。",
                    ),
                    model="audit-model",
                    usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                    finish_reason="stop",
                )
            if model_id != "chat-model":
                raise AssertionError(f"unexpected model: {model_id}")
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="actor-call",
                            name="execute_shell",
                            arguments={
                                "command": "echo actor",
                                "execution_mode": "non_interactive",
                            },
                        )
                    ],
                ),
                model="chat-model",
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                finish_reason="tool_calls",
            )

        async def fake_generate_with_stream_callback(**kwargs: Any) -> InternalResponse:
            response = await fake_generate(**kwargs)
            if response.message.reasoning_content:
                await kwargs["on_reasoning"](response.message.reasoning_content)
            if isinstance(response.message.content, str) and response.message.content:
                await kwargs["on_content"](response.message.content)
            return response

        import app.core.dispatchers.interactive_generation as interactive_generation_module

        monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", fake_generate)
        monkeypatch.setattr(
            interactive_generation_module.LLMClient,
            "generate_with_stream_callback",
            fake_generate_with_stream_callback,
        )
        monkeypatch.setattr(security_module, "get_jwt_secret_key", lambda: "actor-workflow-test-secret")

        yield factory, audit_root, notifier


def _build_authenticated_app(factory: async_sessionmaker[AsyncSession]) -> tuple[FastAPI, str]:
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/v1")

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    token = security_module.create_access_token({"sub": "admin_before"})
    return app, token


async def _submit_http(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    app, token = _build_authenticated_app(factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "message": "run actor command",
                "session_id": SESSION_ID,
                "request_id": "actor-http-request",
                "actor_uid": OWNER_UID,
                "actor_username": "alice",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["data"]["session_id"] == SESSION_ID
    assert payload["data"]["request_id"] == "actor-http-request"
    return int(payload["data"]["work_id"])


async def _submit_websocket(notifier: SessionNotifier) -> int:
    socket = _Socket()
    task = asyncio.create_task(
        chat_api.chat_websocket(
            socket,
            SimpleNamespace(uid=ADMIN_UID, username="admin_before", is_superuser=True),
        )
    )
    await asyncio.wait_for(socket.accepted.wait(), timeout=5)
    try:
        await socket.feed(
            {
                "message": "run actor command",
                "session_id": SESSION_ID,
                "request_id": "actor-ws-request",
                "actor_uid": OWNER_UID,
                "actor_username": "alice",
            }
        )
        accepted = await socket.wait_for_request("actor-ws-request")
        assert accepted["type"] == "input_accepted"
        return int(accepted["work_id"])
    finally:
        await socket.disconnect()
        await asyncio.wait_for(task, timeout=5)
        assert notifier._queues == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "ws"], ids=["http", "websocket"])
async def test_admin_actor_identity_survives_chat_audit_reply_workflow(
    actor_workflow_runtime: tuple[async_sessionmaker[AsyncSession], Path, SessionNotifier],
    transport: str,
) -> None:
    factory, audit_root, notifier = actor_workflow_runtime
    await _seed_actor_session(factory, source=transport)

    if transport == "http":
        work_id = await _submit_http(factory)
    else:
        work_id = await _submit_websocket(notifier)

    async with factory() as db:
        admin = (await db.execute(select(User).where(User.uid == ADMIN_UID))).scalars().one()
        admin.username = "admin_after"
        await db.commit()

    async with factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id="actor-worker",
            lease_seconds=300,
        )
    assert claimed is not None
    assert claimed.id == work_id
    await executor_lifecycle_module.execute_session_reply_work(claimed.id, "actor-worker")

    async with factory() as db:
        records = list((await db.execute(select(AuditRecord).order_by(AuditRecord.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
        session = await db.get(ChatSession, SESSION_ID)
        messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
        details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == records[0].id))).scalars().all()) if records else []
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == records[0].id))).scalars().all()) if records else []

    assert len(records) == 1
    record = records[0]
    assert record.uid == OWNER_UID
    assert record.operator_username == "admin_before"
    assert record.source == transport
    assert record.status == AuditRecordStatus.PENDING
    assert record.decided_by is None
    assert record.context_file_path is not None
    audit_file = Path(record.context_file_path)
    assert audit_file.is_file()
    assert audit_file.parent == audit_root / "temp_owner"

    assert len(works) == 1
    work = works[0]
    assert work.uid == OWNER_UID
    assert work.profile_id == PROFILE_ID
    assert work.status == SessionReplyWorkStatus.SUCCEEDED
    assert work.execution_state["actor_uid"] == ADMIN_UID
    assert work.execution_state["actor_username"] == "admin_before"

    assert session is not None
    assert session.uid == OWNER_UID
    assert session.profile_id == PROFILE_ID
    assert session.profile_override_id == PROFILE_ID
    assert session.source == transport
    assert messages
    assert all(message.uid == OWNER_UID and message.profile_id == PROFILE_ID for message in messages)
    assert any(message.type == MessageType.AUDIT_CONFIRMATION for message in messages)
    assert any(message.type == MessageType.TOOL_RESULT and message.audit_record_id == record.id for message in messages)

    source_tool_message = next(message for message in messages if message.type == MessageType.TOOL_CALL)
    source_internal = InternalMessage.model_validate_json(source_tool_message.content or "{}")
    assert source_internal.tool_calls is not None
    assert [tool_call.id for tool_call in source_internal.tool_calls] == ["actor-call"]
    assert source_internal.tool_calls[0].arguments == {
        "command": "echo actor",
        "execution_mode": "non_interactive",
    }
    assert len(details) == 1
    assert details[0].original_tool_call_id == "actor-call"
    assert details[0].score == 6
    assert details[0].conclusion == AuditToolConclusion.PENDING
    assert executions == []
