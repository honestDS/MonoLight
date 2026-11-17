import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.api.v1 import chat as chat_api
from app.core.audit.confirmation_persistence import persist_pending_confirmation_bundle
from app.core.audit.integrity import build_tool_round_integrity_snapshot
from app.core.security import get_current_user
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.background_task import BackgroundTask
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseProfileBinding
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.provider_usage import ProviderUsageDaily, ProviderUsageDetail
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.system_setting import SystemSetting
from app.models.terminal_session import TerminalSession
from app.models.user import User
from app.providers.database import get_db
from tests.database_support import clone_sqlite_schema
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result


@pytest_asyncio.fixture(name="confirmation_workflow_session_factory")
async def _confirmation_workflow_session_factory(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'confirmation-workflow.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    tables = [
        User.__table__,
        PromptLibrary.__table__,
        Profile.__table__,
        ChatSession.__table__,
        TerminalSession.__table__,
        SessionTodoPlan.__table__,
        KnowledgeBase.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        Message.__table__,
        AuditRecord.__table__,
        AuditToolDetail.__table__,
        AuditConfirmationClaim.__table__,
        AuditExecutionRecord.__table__,
        BackgroundTask.__table__,
        AuditToolResultVersion.__table__,
        SessionReplySequence.__table__,
        SessionReplyWorkItem.__table__,
        SessionReplyWorkCheckpoint.__table__,
        ProviderUsageDetail.__table__,
        ProviderUsageDaily.__table__,
        SystemSetting.__table__,
        SessionReplyStreamEvent.__table__,
        SessionEvent.__table__,
    ]
    await clone_sqlite_schema(tmp_path / "confirmation-workflow.db", tables=tables)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(User(uid="owner", username="owner"))
            await db.commit()
        yield session_factory
    finally:
        await engine.dispose()


def _profile_config() -> dict:
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
        "security": {},
        "tool": {"enabled_tools": ["execute_shell"]},
        "other": {},
        "memory": {},
    }


async def _seed_pending_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    working_directory: Path,
    include_parallel_limit_exceeded_call: bool = False,
    include_initial_user_message: bool = False,
    confirmed_tool_calls: list[InternalToolCall] | None = None,
) -> int:
    uid = "owner"
    session_id = "session-confirmation"
    tool_calls = (
        confirmed_tool_calls
        if confirmed_tool_calls is not None
        else [
            InternalToolCall(
                id="original-shell-call-1",
                name="execute_shell",
                arguments={"command": "echo confirmed-1", "execution_mode": "non_interactive"},
            ),
            InternalToolCall(
                id="original-shell-call-2",
                name="execute_shell",
                arguments={"command": "echo confirmed-2", "execution_mode": "non_interactive"},
            ),
        ]
    )
    parallel_limit_exceeded_call = InternalToolCall(
        id="original-shell-call-over-limit",
        name="execute_shell",
        arguments={"command": "echo over-limit", "execution_mode": "non_interactive"},
    )
    source_tool_calls = [*tool_calls, parallel_limit_exceeded_call] if include_parallel_limit_exceeded_call else tool_calls
    profile_configs = _profile_config()
    if confirmed_tool_calls is not None:
        profile_configs["tool"]["enabled_tools"] = list(dict.fromkeys(tool_call.name for tool_call in tool_calls))
    if include_parallel_limit_exceeded_call:
        profile_configs["tool"]["max_parallel_tools"] = len(tool_calls)
    parallel_limit_exceeded_payload = {
        "status": "failed",
        "tool_name": parallel_limit_exceeded_call.name,
        "error": "parallel_limit_exceeded",
        "requested": len(source_tool_calls),
        "limit": len(tool_calls),
        "executed": False,
        "message": f"Too many parallel tool calls. Requested: {len(source_tool_calls)}, Limit: {len(tool_calls)}.",
    }
    integrity = build_tool_round_integrity_snapshot(
        tool_calls=[{"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments} for tool_call in tool_calls],
        uid=uid,
        session_id=session_id,
        working_directory=working_directory,
    )
    async with session_factory() as db:
        profile = Profile(id=1, uid=uid, name="confirmation workflow", configs=profile_configs)
        db.add(profile)
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                profile_id=1,
                llm_request_metadata={
                    "type": "llm_request_metadata",
                    "turn": 0,
                    "response_id": "confirmation-initial",
                    "input_tokens": 1000,
                    "input_tokens_source": "provider",
                    "cached_tokens": 250,
                    "output_tokens": 200,
                    "total_input_tokens": 1000,
                    "total_cached_tokens": 250,
                    "total_output_tokens": 200,
                    "cache_hit_rate": 0.25,
                    "context_window_tokens": 4096,
                    "max_output_tokens": 512,
                },
            )
        )
        await db.flush()

        if include_initial_user_message:
            db.add(
                Message(
                    session_id=session_id,
                    uid=uid,
                    profile_id=1,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content="Run the proposed shell commands.",
                    is_processed=True,
                )
            )
            await db.flush()

        source = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=source_tool_calls).model_dump_json(exclude_none=True),
            is_processed=True,
        )
        db.add(source)
        await db.flush()

        record = AuditRecord(
            uid=uid,
            operator_username="owner",
            session_id=session_id,
            source="http",
            language="zh",
            status=AuditRecordStatus.PENDING,
            source_assistant_message_id=source.id,
            working_directory=str(working_directory),
            round_arguments_hash=integrity.round_sha256,
            tool_count=len(tool_calls),
            intent_summary="execute confirmed shell commands",
            pending_at=get_local_time(),
            expires_at=get_local_time() + timedelta(hours=1),
        )
        db.add(record)
        await db.flush()
        for turn_index, (tool_call, snapshot) in enumerate(zip(tool_calls, integrity.tool_calls, strict=True)):
            db.add(
                AuditToolDetail(
                    audit_record_id=record.id,
                    original_tool_call_id=tool_call.id,
                    turn_index=turn_index,
                    tool_name=tool_call.name,
                    conclusion=AuditToolConclusion.PENDING,
                    score=1,
                    reason="confirmation workflow",
                    arguments_hash=snapshot.arguments_sha256,
                    arguments_summary=json.dumps(tool_call.arguments, ensure_ascii=False),
                    file_snapshots=[],
                )
            )
        await db.flush()

        if include_parallel_limit_exceeded_call:
            await save_message(
                db,
                session_id,
                uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=parallel_limit_exceeded_call.id,
                    content=json.dumps(
                        parallel_limit_exceeded_payload,
                        ensure_ascii=False,
                    ),
                ),
                1,
                is_processed=True,
                commit=False,
            )

        pending_results = [pending_audit_tool_result(call.id) for call in tool_calls]
        await persist_pending_confirmation_bundle(
            db,
            audit_record_id=record.id,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            tool_results=pending_results,
            confirmation_payload=audit_confirmation_payload(
                record.id,
                summary=record.intent_summary,
                risk=1,
                expires_at=record.expires_at.isoformat(),
                timeout_seconds=3600,
                locale=record.language,
            ),
            dedupe_key=f"audit-confirmation:{record.id}",
        )
        assert record.id is not None
        return record.id


async def _wait_for_confirmed_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> SessionReplyWorkItem:
    for _ in range(200):
        async with session_factory() as db:
            result = await db.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.session_id == "session-confirmation",
                    SessionReplyWorkItem.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                )
            )
            work = result.scalars().first()
            if work is not None:
                return work
        await asyncio.sleep(0.01)
    raise AssertionError("confirmed tool work was not enqueued")


async def _stop_confirmation_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str = "session-confirmation",
    uid: str = "owner",
) -> dict:
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/v1")

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db:
            yield db

    def override_current_user() -> SimpleNamespace:
        return SimpleNamespace(uid=uid, is_superuser=False)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/chat/sessions/stop", params={"session_id": session_id})
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    return payload["data"]
