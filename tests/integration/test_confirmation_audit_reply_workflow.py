import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.dispatchers.interactive_runtime as interactive_runtime_module
import app.core.dispatchers.memory.request as memory_request_module
import app.providers.database as database_provider
from app.adapters.chat_web import web_chat_adapter
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.api.v1 import chat as chat_api
from app.core.audit import confirmation_events as confirmation_events_module
from app.core.audit import service as audit_service_module
from app.core.audit.confirmation_persistence import persist_pending_confirmation_bundle
from app.core.audit.integrity import build_tool_round_integrity_snapshot
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT
from app.core.security import get_current_user
from app.core.session.notifier import SessionNotifier
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_audit as executor_audit_module
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue import manager_result as manager_result_module
from app.core.session_reply_queue import manager_submission as manager_submission_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.tools import MANAGE_TODO_TOOL_NAME, TOOL_EXECUTOR_MAP
from app.core.utils.dispatcher import channel_call as channel_call_module
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.request_token_baseline import build_provider_request_usage_metadata
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.background_task import BackgroundTask
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseProfileBinding
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_provider_usage import SessionReplyProviderRequestPurpose, SessionReplyProviderUsage
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.terminal_session import TerminalSession
from app.models.user import User
from app.providers.database import get_db
from app.providers.llm.client import LLMClient
from tests.database_support import clone_sqlite_schema
from tests.integration.test_chat_websocket_permissions import _Socket


@pytest_asyncio.fixture
async def confirmation_workflow_session_factory(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
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
        SessionReplyProviderUsage.__table__,
        SessionReplyStreamEvent.__table__,
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
) -> int:
    uid = "owner"
    session_id = "session-confirmation"
    tool_calls = [
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
    parallel_limit_exceeded_call = InternalToolCall(
        id="original-shell-call-over-limit",
        name="execute_shell",
        arguments={"command": "echo over-limit", "execution_mode": "non_interactive"},
    )
    source_tool_calls = [*tool_calls, parallel_limit_exceeded_call] if include_parallel_limit_exceeded_call else tool_calls
    profile_configs = _profile_config()
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

        pending_results = [
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                content=json.dumps(
                    {
                        "status": AuditRecordStatus.PENDING.value,
                        "error": "waiting for confirmation",
                        "reason": "confirmation required",
                    },
                    ensure_ascii=False,
                ),
            )
            for tool_call in tool_calls
        ]
        await persist_pending_confirmation_bundle(
            db,
            audit_record_id=record.id,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            tool_results=pending_results,
            confirmation_payload={
                "type": "audit_confirmation",
                "audit_record_id": record.id,
                "status": "pending",
                "confirmation_mode": "standard",
            },
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


@pytest.mark.asyncio
async def test_confirmation_workflow_approves_executes_replaces_pending_result_and_completes_reply(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        include_parallel_limit_exceeded_call=True,
    )

    monkeypatch.setattr(database_provider, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(manager_result_module, "WORK_RESULT_POLL_INTERVAL_SECONDS", 0.01)

    async def resolve_profile(db: AsyncSession, *, uid: str, session_id: str):
        profile = await db.get(Profile, 1)
        assert profile is not None and profile.uid == uid and session_id == "session-confirmation"
        return profile

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    async def ensure_writable(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)

    executed_calls: list[InternalToolCall] = []
    execution_messages: list[list[InternalMessage]] = []

    async def execute_tool(tool_call, _db, _profile, _cfg, messages, _username, _session_id, _turn, _uid, **_kwargs):
        executed_calls.append(tool_call.model_copy(deep=True))
        execution_messages.append([message.model_copy(deep=True) for message in messages])
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps(
                {
                    "status": "success",
                    "stdout": f"{tool_call.arguments['command']}\n",
                    "exit_code": 0,
                }
            ),
        )

    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", execute_tool)

    resumed_histories: list[list[Message]] = []

    async def continue_reply(**kwargs) -> dict:
        db = kwargs["db"]
        result = await db.execute(select(Message).where(Message.session_id == kwargs["session_id"]).order_by(Message.id))
        persisted = list(result.scalars().all())
        resumed_histories.append(persisted)
        pending_rows = [row for row in persisted if row.type == MessageType.TOOL_RESULT and row.audit_record_id == audit_record_id]
        assert [row.audit_tool_call_id for row in pending_rows] == [
            "original-shell-call-1",
            "original-shell-call-2",
        ]
        assert [row.content_revision for row in pending_rows] == [2, 2]
        replacement_payloads = []
        for pending_row in pending_rows:
            pending_internal = InternalMessage.model_validate_json(pending_row.content or "{}")
            assert pending_internal.tool_call_id == pending_row.audit_tool_call_id
            replacement_payloads.append(json.loads(pending_internal.content))
        assert [payload["status"] for payload in replacement_payloads] == ["success", "success"]
        assert [payload["stdout"] for payload in replacement_payloads] == [
            "echo confirmed-1\n",
            "echo confirmed-2\n",
        ]
        parallel_limit_rows = [row for row in persisted if row.type == MessageType.TOOL_RESULT and row.audit_record_id is None]
        assert len(parallel_limit_rows) == 1
        assert parallel_limit_rows[0].audit_tool_call_id is None
        assert parallel_limit_rows[0].content_revision == 0
        parallel_limit_result = InternalMessage.model_validate_json(parallel_limit_rows[0].content or "{}")
        assert parallel_limit_result.tool_call_id == "original-shell-call-over-limit"
        assert json.loads(parallel_limit_result.content or "{}") == {
            "status": "failed",
            "tool_name": "execute_shell",
            "error": "parallel_limit_exceeded",
            "requested": 3,
            "limit": 2,
            "executed": False,
            "message": "Too many parallel tool calls. Requested: 3, Limit: 2.",
        }

        await db.commit()
        metrics = {
            "input_tokens": 120,
            "input_tokens_source": "provider",
            "cached_tokens": 20,
            "output_tokens": 7,
        }
        await kwargs["request_metadata_callback"](
            {
                "type": "llm_request_metadata",
                "turn": 1,
                "response_id": "confirmation-final",
                "input_tokens": metrics["input_tokens"],
                "input_tokens_source": metrics["input_tokens_source"],
                "cached_tokens": metrics["cached_tokens"],
                "output_tokens": metrics["output_tokens"],
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                **build_provider_request_usage_metadata("confirmed-continuation-request", metrics),
            }
        )
        async with confirmation_workflow_session_factory() as metadata_db:
            session = await metadata_db.get(ChatSession, kwargs["session_id"])
            assert session is not None
            latest_llm_request_metadata = session.llm_request_metadata

        await save_message(
            db,
            kwargs["session_id"],
            kwargs["uid"],
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="confirmed tool completed"),
            kwargs["persisted_profile_id"],
            dedupe_key=kwargs["final_message_dedupe_key"],
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "confirmed tool completed"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
            "response_id": "confirmation-final",
            "llm_request_metadata": latest_llm_request_metadata,
        }

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", continue_reply)

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    async with confirmation_workflow_session_factory() as web_db:
        response_task = asyncio.create_task(
            web_chat_adapter.chat(
                web_db,
                "approve",
                uid="owner",
                session_id="session-confirmation",
                request_id="confirmation-request",
            )
        )
        created_work = await _wait_for_confirmed_work(confirmation_workflow_session_factory)
        async with confirmation_workflow_session_factory() as worker_db:
            claimed = await session_reply_work_item_crud.claim_next(
                worker_db,
                worker_id="confirmation-worker",
                lease_seconds=300,
            )
        assert claimed is not None
        assert claimed.id == created_work.id
        assert claimed.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
        await executor_lifecycle_module.execute_session_reply_work(claimed.id, "confirmation-worker")
        response = await asyncio.wait_for(response_task, timeout=5)

    assert response["content"] == "confirmed tool completed"
    assert response["work_id"] == created_work.id
    assert len(executed_calls) == 2
    assert [call.name for call in executed_calls] == ["execute_shell", "execute_shell"]
    assert [call.arguments for call in executed_calls] == [
        {"command": "echo confirmed-1", "execution_mode": "non_interactive"},
        {"command": "echo confirmed-2", "execution_mode": "non_interactive"},
    ]
    fresh_call_ids = [call.id for call in executed_calls]
    assert len(set(fresh_call_ids)) == 2
    assert all(call_id.startswith("call_") for call_id in fresh_call_ids)
    assert set(fresh_call_ids).isdisjoint({"original-shell-call-1", "original-shell-call-2"})
    assert [message.role for message in execution_messages[1]] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert execution_messages[1][0].tool_calls is not None
    assert execution_messages[1][0].tool_calls[0].id == fresh_call_ids[0]
    assert execution_messages[1][1].tool_call_id == fresh_call_ids[0]
    assert resumed_histories

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        work = await session_reply_work_item_crud.get(db, created_work.id)
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.version_no))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        usage = (await db.execute(select(SessionReplyProviderUsage).where(SessionReplyProviderUsage.provider_request_id == "confirmed-continuation-request"))).scalars().one_or_none()
        session = await db.get(ChatSession, "session-confirmation")
        source_message = await db.get(Message, record.source_assistant_message_id) if record is not None and record.source_assistant_message_id is not None else None

    assert record is not None
    assert source_message is not None
    source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    assert [tool_call.id for tool_call in source_internal.tool_calls or []] == [
        "original-shell-call-1",
        "original-shell-call-2",
        "original-shell-call-over-limit",
    ]
    assert record.status == AuditRecordStatus.SUCCEEDED
    assert record.decision is not None and record.decision.value == "approve"
    assert record.decision_raw_message == "approve"
    assert record.execution_claim_token is None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.SUCCEEDED
    assert work.result_message_id is not None
    assert len(executions) == 2
    assert [execution.status for execution in executions] == [
        AuditExecutionStatus.SUCCEEDED,
        AuditExecutionStatus.SUCCEEDED,
    ]
    assert [execution.new_tool_call_id for execution in executions] == fresh_call_ids
    versions_by_call: dict[str, list[AuditToolResultVersion]] = {}
    for version in versions:
        versions_by_call.setdefault(version.original_tool_call_id, []).append(version)
    assert set(versions_by_call) == {"original-shell-call-1", "original-shell-call-2"}
    for original_call_id, call_versions in versions_by_call.items():
        assert [version.version_no for version in call_versions] == [0, 1, 2]
        final_payload = json.loads(InternalMessage.model_validate_json(call_versions[-1].content).content)
        assert final_payload["status"] == "success"
        expected_command = "echo confirmed-1" if original_call_id.endswith("-1") else "echo confirmed-2"
        assert final_payload["stdout"] == f"{expected_command}\n"
    parallel_limit_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT and message.audit_record_id is None]
    assert len(parallel_limit_rows) == 1
    assert parallel_limit_rows[0].audit_tool_call_id is None
    assert parallel_limit_rows[0].content_revision == 0
    parallel_limit_result = InternalMessage.model_validate_json(parallel_limit_rows[0].content or "{}")
    assert parallel_limit_result.tool_call_id == "original-shell-call-over-limit"
    assert json.loads(parallel_limit_result.content or "{}") == {
        "status": "failed",
        "tool_name": "execute_shell",
        "error": "parallel_limit_exceeded",
        "requested": 3,
        "limit": 2,
        "executed": False,
        "message": "Too many parallel tool calls. Requested: 3, Limit: 2.",
    }
    final_message = next(message for message in messages if message.id == work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.content == "confirmed tool completed"
    assert any(event.get("source") == "confirmed_tool_execution" for event in delivered_events)
    assert usage is not None
    assert usage.request_purpose == SessionReplyProviderRequestPurpose.MAIN_DIALOGUE
    assert usage.work_id == created_work.id
    assert usage.input_tokens == 120
    assert usage.cached_tokens == 20
    assert usage.output_tokens == 7
    assert session is not None
    assert session.llm_request_metadata["total_input_tokens"] == 1120
    assert session.llm_request_metadata["total_cached_tokens"] == 270
    assert session.llm_request_metadata["total_output_tokens"] == 207
    assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(270 / 1120)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "high_risk", "expected_status", "expected_work_type"),
    [
        pytest.param(
            "同意",
            False,
            AuditRecordStatus.EXECUTING,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            id="approve-normal",
        ),
        pytest.param(
            "忽略",
            True,
            AuditRecordStatus.EXECUTING,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            id="ignore-high-risk",
        ),
        pytest.param(
            "拒绝",
            True,
            AuditRecordStatus.REJECTED,
            SessionReplyWorkType.FOREGROUND_REPLY,
            id="reject-high-risk",
        ),
    ],
)
async def test_weixin_confirmation_decision_restores_username_when_actor_is_omitted(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    high_risk: bool,
    expected_status: AuditRecordStatus,
    expected_work_type: SessionReplyWorkType,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        user = (await db.execute(select(User).where(User.uid == "owner"))).scalars().one()
        user.username = "weixin_user"
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        record.operator_username = "original_initiator"
        record.source = "weixin-openclaw"
        session = await db.get(ChatSession, "session-confirmation")
        assert session is not None
        session.source = "weixin-openclaw"
        session.reply_target_source = "weixin-openclaw"
        session.profile_override_id = 1
        if high_risk:
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
            assert details
            for detail in details:
                detail.score = 8
        await db.commit()

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.ChatDispatcher.validate_initial_message_before_save", validate_initial)

    async def send_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(confirmation_events_module, "send_session_event", send_session_event)

    adapter = WeixinOpenClawAdapter(WeixinOpenClawConfig())
    try:
        async with confirmation_workflow_session_factory() as db:
            result = await adapter.chat(
                db,
                message,
                uid="owner",
                session_id="session-confirmation",
            )
    finally:
        await adapter.close()

    assert result.text == ""
    assert result.files == []

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        decision_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.AUDIT_DECISION,
                    )
                )
            )
            .scalars()
            .all()
        )
        works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == "session-confirmation",
                        SessionReplyWorkItem.uid == "owner",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert record is not None
    assert record.decided_by == "weixin_user"
    assert record.operator_username == "original_initiator"
    assert record.uid == "owner"
    assert record.status == expected_status
    assert record.decision is not None
    assert record.decision.value == ("reject" if message == "拒绝" else "approve")
    assert record.decision_raw_message == message
    assert len(decision_messages) == 1
    decision_message = decision_messages[0]
    assert decision_message.id == record.decision_message_id
    assert decision_message.uid == "owner"
    assert decision_message.profile_id == 1
    assert decision_message.content == message
    assert len(works) == 1
    work = works[0]
    assert work.uid == "owner"
    assert work.profile_id == 1
    assert work.work_type == expected_work_type
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert work.execution_state["actor_uid"] == "owner"
    assert work.execution_state["actor_username"] == "weixin_user"
    assert work.execution_state["message_source"] == "weixin-openclaw"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_audit_status", "expected_message_type", "expected_work_type", "expected_decision"),
    [
        pytest.param(
            "approve",
            AuditRecordStatus.EXECUTING,
            MessageType.AUDIT_DECISION,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            "approve",
            id="approve",
        ),
        pytest.param(
            "reject",
            AuditRecordStatus.REJECTED,
            MessageType.AUDIT_DECISION,
            SessionReplyWorkType.FOREGROUND_REPLY,
            "reject",
            id="reject",
        ),
        pytest.param(
            "invalid confirmation",
            AuditRecordStatus.CANCELLED,
            MessageType.TEXT,
            SessionReplyWorkType.FOREGROUND_REPLY,
            None,
            id="invalid-input",
        ),
    ],
)
async def test_web_submit_duplicate_request_id_does_not_repeat_confirmation_decision_or_enqueue(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
    expected_audit_status: AuditRecordStatus,
    expected_message_type: MessageType,
    expected_work_type: SessionReplyWorkType,
    expected_decision: str | None,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async def resolve_profile(db: AsyncSession, *, uid: str, session_id: str):
        profile = await db.get(Profile, 1)
        assert profile is not None and profile.uid == uid and session_id == "session-confirmation"
        return profile

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    async def ensure_writable(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)

    async def capture_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    request_id = "confirmation-submit-duplicate"
    expire_call_count = 0
    original_expire_confirmation = manager_submission_module.expire_confirmation_by_session

    async def expire_before_idempotent_reservation(db: AsyncSession, *, uid: str, session_id: str) -> int:
        nonlocal expire_call_count
        expire_call_count += 1
        provisional_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == session_id,
                        Message.uid == uid,
                        Message.dedupe_key.like("http:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert provisional_messages == []
        return await original_expire_confirmation(db, uid=uid, session_id=session_id)

    monkeypatch.setattr(
        manager_submission_module,
        "expire_confirmation_by_session",
        expire_before_idempotent_reservation,
    )

    async with confirmation_workflow_session_factory() as first_db:
        first_response = await web_chat_adapter.submit(
            first_db,
            message,
            attachments=[],
            uid="owner",
            session_id="session-confirmation",
            request_id=request_id,
        )

    async def load_state():
        async with confirmation_workflow_session_factory() as db:
            record = await db.get(AuditRecord, audit_record_id)
            messages = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == "session-confirmation",
                            Message.uid == "owner",
                            Message.role == MessageRole.USER,
                        )
                    )
                )
                .scalars()
                .all()
            )
            works = list(
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.session_id == "session-confirmation",
                            SessionReplyWorkItem.uid == "owner",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert record is not None
            return record, messages, works

    first_record, first_messages, first_works = await load_state()
    first_audit_state = (
        first_record.status,
        first_record.decision,
        first_record.decision_message_id,
        first_record.decision_raw_message,
        first_record.decided_by,
        first_record.execution_claim_token,
        first_record.error_reason,
        first_record.completed_at,
        first_record.updated_at,
    )

    async with confirmation_workflow_session_factory() as second_db:
        second_response = await web_chat_adapter.submit(
            second_db,
            message,
            attachments=[],
            uid="owner",
            session_id="session-confirmation",
            request_id=request_id,
        )

    second_record, second_messages, second_works = await load_state()

    assert expire_call_count == 1
    assert first_response["request_id"] == second_response["request_id"] == request_id
    assert first_response["work_id"] == second_response["work_id"]
    assert second_response["session_events"] == []
    assert first_record.status == expected_audit_status
    if expected_decision is None:
        assert first_record.decision is None
    else:
        assert first_record.decision is not None and first_record.decision.value == expected_decision
    assert (
        second_record.status,
        second_record.decision,
        second_record.decision_message_id,
        second_record.decision_raw_message,
        second_record.decided_by,
        second_record.execution_claim_token,
        second_record.error_reason,
        second_record.completed_at,
        second_record.updated_at,
    ) == first_audit_state

    assert len(first_messages) == len(second_messages) == 1
    message_row = first_messages[0]
    assert message_row.type == expected_message_type
    assert message_row.content == message
    assert len(first_works) == len(second_works) == 1
    work = first_works[0]
    assert work.id == first_response["work_id"] == second_response["work_id"] == second_works[0].id
    assert work.work_type == expected_work_type
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    if expected_work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
        assert work.source_id == str(audit_record_id)
        assert work.execution_state["decision_message_id"] == message_row.id
    else:
        assert work.source_id == str(message_row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    [
        pytest.param(("admin", "admin_name", True), id="admin"),
        pytest.param(("owner", "owner", False), id="owner"),
    ],
)
@pytest.mark.parametrize(
    ("message", "risk_level", "high_risk", "expected_status", "expected_work_type"),
    [
        pytest.param(
            "approve",
            "normal",
            False,
            AuditRecordStatus.EXECUTING,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            id="approve-normal",
        ),
        pytest.param(
            "ignore",
            "high-risk",
            True,
            AuditRecordStatus.EXECUTING,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            id="ignore-high-risk",
        ),
        pytest.param(
            "reject",
            "high-risk",
            True,
            AuditRecordStatus.REJECTED,
            SessionReplyWorkType.FOREGROUND_REPLY,
            id="reject-high-risk",
        ),
    ],
)
async def test_http_confirmation_decision_uses_authenticated_actor_and_preserves_owner_provenance(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actor: tuple[str, str, bool],
    message: str,
    risk_level: str,
    high_risk: bool,
    expected_status: AuditRecordStatus,
    expected_work_type: SessionReplyWorkType,
) -> None:
    actor_uid, actor_username, actor_is_superuser = actor
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        db.add(User(uid="admin", username="admin_name", is_superuser=True))
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        session = await db.get(ChatSession, "session-confirmation")
        assert session is not None
        session.profile_override_id = 1
        record.operator_username = "original_initiator"
        if high_risk:
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
            assert details
            for detail in details:
                detail.score = 8
        await db.commit()

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial)

    async def capture_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api/v1")

    current_actor = {
        "uid": actor_uid,
        "username": actor_username,
        "is_superuser": actor_is_superuser,
    }

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with confirmation_workflow_session_factory() as db:
            yield db

    def override_current_user() -> SimpleNamespace:
        return SimpleNamespace(**current_actor)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    request_payload = {
        "session_id": "session-confirmation",
        "message": message,
        "request_id": "http-confirmation-actor-provenance",
        "actor_uid": "forged",
        "actor_username": "forged",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first_response = await client.post("/api/v1/chat/completions", json=request_payload)
        assert first_response.status_code == 200

        async with confirmation_workflow_session_factory() as db:
            first_record = await db.get(AuditRecord, audit_record_id)
            first_decision_messages = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == "session-confirmation",
                            Message.type == MessageType.AUDIT_DECISION,
                        )
                    )
                )
                .scalars()
                .all()
            )
            first_works = list(
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.session_id == "session-confirmation",
                            SessionReplyWorkItem.uid == "owner",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert first_record is not None
            assert len(first_decision_messages) == 1
            assert len(first_works) == 1
            first_audit_snapshot = (first_record.decided_by, first_record.decision_message_id)
            first_work_snapshot = (first_works[0].id, dict(first_works[0].execution_state or {}))

        if actor_uid == "admin":
            current_actor.update(uid="owner", username="owner", is_superuser=False)
        second_response = await client.post("/api/v1/chat/completions", json=request_payload)
        assert second_response.status_code == 200

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        decision_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.AUDIT_DECISION,
                    )
                )
            )
            .scalars()
            .all()
        )
        session = await db.get(ChatSession, "session-confirmation")
        works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == "session-confirmation",
                        SessionReplyWorkItem.uid == "owner",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert record is not None
    assert record.uid == "owner"
    assert record.operator_username == "original_initiator"
    assert record.decided_by == actor_username
    assert record.decision is not None
    assert record.decision.value == ("reject" if message == "reject" else "approve")
    assert record.decision_raw_message == message
    assert record.status == expected_status, risk_level
    assert len(decision_messages) == 1
    decision_message = decision_messages[0]
    assert decision_message.uid == "owner"
    assert decision_message.profile_id == 1
    assert decision_message.content == message
    assert record.decision_message_id == decision_message.id
    assert session is not None
    assert session.uid == "owner"
    assert session.profile_id == 1
    assert len(works) == 1
    work = works[0]
    assert work.uid == "owner"
    assert work.profile_id == 1
    assert work.work_type == expected_work_type
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert work.execution_state["actor_uid"] == actor_uid
    assert work.execution_state["actor_username"] == actor_username
    assert (record.decided_by, record.decision_message_id) == first_audit_snapshot
    assert (work.id, work.execution_state) == first_work_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("active_stream", [False, True], ids=["initial-stream", "active-stream"])
@pytest.mark.parametrize(
    ("message", "high_risk", "expected_status"),
    [
        pytest.param("approve", False, AuditRecordStatus.EXECUTING, id="approve-normal"),
        pytest.param("ignore", True, AuditRecordStatus.EXECUTING, id="ignore-high-risk"),
        pytest.param("reject", True, AuditRecordStatus.REJECTED, id="reject-high-risk"),
    ],
)
async def test_websocket_confirmation_decision_uses_authenticated_actor_and_preserves_owner_provenance(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_stream: bool,
    message: str,
    high_risk: bool,
    expected_status: AuditRecordStatus,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        db.add(User(uid="admin", username="admin_name", is_superuser=True))
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        session = await db.get(ChatSession, "session-confirmation")
        assert session is not None
        session.profile_override_id = 1
        record.operator_username = "owner"
        if high_risk:
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
            assert details
            for detail in details:
                detail.score = 8
        await db.commit()

    monkeypatch.setattr(chat_api, "AsyncSessionLocal", confirmation_workflow_session_factory)

    async def runtime_settings(_db: AsyncSession) -> SimpleNamespace:
        return SimpleNamespace(log_locale="zh")

    monkeypatch.setattr(chat_api.system_setting_crud, "get_runtime_settings", runtime_settings)

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial)

    async def send_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(confirmation_events_module, "send_session_event", send_session_event)
    monkeypatch.setattr(chat_api, "session_notifier", SessionNotifier())

    stream_blocked = asyncio.Event()
    resume_started = asyncio.Event()

    async def wait_for_stream(_work_id: int, **_kwargs):
        await stream_blocked.wait()
        if False:
            yield {}

    async def wait_for_session_stream(*_args, **_kwargs):
        resume_started.set()
        await stream_blocked.wait()
        if False:
            yield {}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", wait_for_stream)
    monkeypatch.setattr(session_reply_queue_manager, "wait_for_session_stream", wait_for_session_stream)

    socket = _Socket()
    websocket_task = asyncio.create_task(
        chat_api.chat_websocket(
            socket,
            SimpleNamespace(uid="admin", username="admin_name", is_superuser=True),
        )
    )
    request_id = f"ws-confirmation-{message}-{'active' if active_stream else 'initial'}"
    try:
        await asyncio.wait_for(socket.accepted.wait(), timeout=5)
        if active_stream:
            await socket.feed({"type": "resume", "session_id": "session-confirmation"})
            await asyncio.wait_for(resume_started.wait(), timeout=5)
        await socket.feed(
            {
                "message": message,
                "session_id": "session-confirmation",
                "request_id": request_id,
                "actor_uid": "forged",
                "actor_username": "forged",
            }
        )
        accepted = await socket.wait_for(lambda event: event.get("type") == "input_accepted" and event.get("request_id") == request_id)
        assert accepted["submission_status"] == ("rejected" if message == "reject" else "approved")
    finally:
        await socket.disconnect()
        await asyncio.wait_for(websocket_task, timeout=5)

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        decision_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.AUDIT_DECISION,
                    )
                )
            )
            .scalars()
            .all()
        )
        work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == "session-confirmation",
                        SessionReplyWorkItem.uid == "owner",
                    )
                )
            )
            .scalars()
            .all()
        )
        session = await db.get(ChatSession, "session-confirmation")

    assert record is not None
    assert record.uid == "owner"
    assert record.operator_username == "owner"
    assert record.decided_by == "admin_name"
    assert record.decision is not None
    assert record.decision.value == ("reject" if message == "reject" else "approve")
    assert record.decision_raw_message == message
    assert record.status == expected_status
    assert len(decision_messages) == 1
    decision_message = decision_messages[0]
    assert decision_message.uid == "owner"
    assert decision_message.profile_id == 1
    assert decision_message.content == message
    assert record.decision_message_id == decision_message.id
    assert session is not None
    assert session.uid == "owner"
    assert session.profile_id == 1
    assert session.profile_override_id == 1
    assert len(work_items) == 1
    work = work_items[0]
    expected_work_type = SessionReplyWorkType.FOREGROUND_REPLY if message == "reject" else SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert work.uid == "owner"
    assert work.profile_id == 1
    assert work.work_type == expected_work_type
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert work.execution_state["actor_uid"] == "admin"
    assert work.execution_state["actor_username"] == "admin_name"
    assert work.execution_state["message_source"] == "ws"


@pytest.mark.asyncio
async def test_high_risk_confirmation_stop_before_worker_claim_cancels_audit_bundle(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
        assert details
        for detail in details:
            detail.score = 9
        await db.commit()
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        source_message_id = record.source_assistant_message_id
        confirmation_card = (await db.execute(select(Message).where(Message.session_id == "session-confirmation", Message.uid == "owner", Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().one()
        assert confirmation_card.id is not None
        confirmation_card_id = confirmation_card.id

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="ignore",
            attachments=None,
            source="http",
        )
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        assert record.status == AuditRecordStatus.EXECUTING
    assert submission_status == "approved"
    assert work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM

    stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
    assert stop_payload == {"session_id": "session-confirmation", "cancelled_count": 1}

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        stopped_work = await db.get(SessionReplyWorkItem, work.id)
        source_message = await db.get(Message, source_message_id)
        decision_message = await db.get(Message, record.decision_message_id) if record and record.decision_message_id else None
        confirmation_card = await db.get(Message, confirmation_card_id)
        structured_results = list((await db.execute(select(Message).where(Message.audit_record_id == audit_record_id, Message.audit_tool_call_id.is_not(None), Message.type == MessageType.TOOL_RESULT).order_by(Message.id))).scalars().all())
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())

    assert record is not None
    assert record.status == AuditRecordStatus.CANCELLED
    assert record.completed_at is not None
    completed_at = record.completed_at
    assert record.execution_claim_token is None
    assert record.decision is not None and record.decision.value == "approve"
    assert record.decision_raw_message == "ignore"
    assert source_message is not None and source_message.type == MessageType.TOOL_CALL
    assert decision_message is not None
    assert decision_message.type == MessageType.AUDIT_DECISION
    assert decision_message.content == "ignore"
    assert stopped_work is not None
    assert stopped_work.status == SessionReplyWorkStatus.CANCELLED
    assert stopped_work.locked_by is None and stopped_work.lock_until is None
    assert confirmation_card is not None
    assert json.loads(confirmation_card.content or "{}")["status"] == AuditRecordStatus.CANCELLED.value
    assert len(structured_results) == 2
    for result_message in structured_results:
        tool_result = InternalMessage.model_validate_json(result_message.content or "{}")
        result_payload = json.loads(tool_result.content or "{}")
        assert result_payload["status"] == AuditRecordStatus.CANCELLED.value
        assert result_payload["confirmation_status"] == AuditRecordStatus.CANCELLED.value
        assert result_payload["confirmation_decision"] == "ignore"
    assert claims == []
    assert executions == []
    versions_by_call: dict[str, list[AuditToolResultVersion]] = {}
    for version in versions:
        versions_by_call.setdefault(version.original_tool_call_id, []).append(version)
    assert set(versions_by_call) == {"original-shell-call-1", "original-shell-call-2"}
    assert all([version.version_no for version in call_versions] == [0, 1, 2] for call_versions in versions_by_call.values())
    assert any(event.get("type") == "audit_confirmation_status" and event.get("audit_record_id") == audit_record_id and event.get("status") == AuditRecordStatus.CANCELLED.value for event in delivered_events)

    repeated_stop = await _stop_confirmation_session(confirmation_workflow_session_factory)
    assert repeated_stop == {"session_id": "session-confirmation", "cancelled_count": 0}
    async with confirmation_workflow_session_factory() as db:
        repeated_record = await db.get(AuditRecord, audit_record_id)
        repeated_versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())
    assert repeated_record is not None
    assert repeated_record.completed_at == completed_at
    assert [(version.original_tool_call_id, version.version_no) for version in repeated_versions] == [(version.original_tool_call_id, version.version_no) for version in versions]

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, replacement_work, _submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="ignore",
            attachments=None,
            source="http",
        )
    assert replacement_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert replacement_work.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_stop_pending_confirmation_without_work_cancels_bundle_without_enqueueing(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        source_message_id = record.source_assistant_message_id
        confirmation_card = (await db.execute(select(Message).where(Message.session_id == "session-confirmation", Message.uid == "owner", Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().one()
        assert confirmation_card.id is not None
        confirmation_card_id = confirmation_card.id
        pending_tool_messages = list((await db.execute(select(Message).where(Message.audit_record_id == audit_record_id, Message.audit_tool_call_id.is_not(None), Message.type == MessageType.TOOL_RESULT).order_by(Message.id))).scalars().all())
        assert len(pending_tool_messages) == 2
        assert all(message.id is not None and message.audit_tool_call_id is not None for message in pending_tool_messages)
        tool_message_ids = [message.id for message in pending_tool_messages]
        tool_call_ids = [message.audit_tool_call_id for message in pending_tool_messages]
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())
        version_counts: dict[str, int] = {tool_call_id: 0 for tool_call_id in tool_call_ids if tool_call_id is not None}
        for version in versions:
            version_counts[version.original_tool_call_id] += 1
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-confirmation", SessionReplyWorkItem.uid == "owner"))).scalars().all())

    assert works == []
    assert version_counts == {"original-shell-call-1": 1, "original-shell-call-2": 1}

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
    assert stop_payload == {"session_id": "session-confirmation", "cancelled_count": 0}

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        source_message = await db.get(Message, source_message_id)
        confirmation_card = await db.get(Message, confirmation_card_id)
        pending_tool_messages = list((await db.execute(select(Message).where(Message.audit_record_id == audit_record_id, Message.audit_tool_call_id.is_not(None), Message.type == MessageType.TOOL_RESULT).order_by(Message.id))).scalars().all())
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-confirmation", SessionReplyWorkItem.uid == "owner"))).scalars().all())

    assert record is not None
    assert record.status == AuditRecordStatus.CANCELLED
    assert record.completed_at is not None
    assert record.execution_claim_token is None
    assert source_message is not None and source_message.id == source_message_id
    assert confirmation_card is not None and confirmation_card.id == confirmation_card_id
    assert json.loads(confirmation_card.content or "{}")["status"] == AuditRecordStatus.CANCELLED.value
    assert [message.id for message in pending_tool_messages] == tool_message_ids
    assert [message.audit_tool_call_id for message in pending_tool_messages] == tool_call_ids
    for message in pending_tool_messages:
        tool_result = InternalMessage.model_validate_json(message.content or "{}")
        result_payload = json.loads(tool_result.content or "{}")
        assert result_payload["status"] == AuditRecordStatus.CANCELLED.value
        assert result_payload["confirmation_status"] == AuditRecordStatus.CANCELLED.value
    version_counts_after: dict[str, int] = {tool_call_id: 0 for tool_call_id in tool_call_ids if tool_call_id is not None}
    for version in versions:
        version_counts_after[version.original_tool_call_id] += 1
    assert version_counts_after == {tool_call_id: count + 1 for tool_call_id, count in version_counts.items()}
    assert claims == []
    assert executions == []
    assert works == []
    assert any(event.get("type") == "audit_confirmation_status" and event.get("audit_record_id") == audit_record_id and event.get("status") == AuditRecordStatus.CANCELLED.value for event in delivered_events)

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        approval_message, foreground_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="approve",
            attachments=None,
            source="http",
        )
    assert submission_status == "accepted"
    assert foreground_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert foreground_work.source_type == SessionReplySourceType.USER_MESSAGE
    assert foreground_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert approval_message.id is not None
    assert foreground_work.source_id == str(approval_message.id)

    async with confirmation_workflow_session_factory() as db:
        record_after_approval = await db.get(AuditRecord, audit_record_id)
        works_after_approval = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-confirmation", SessionReplyWorkItem.uid == "owner").order_by(SessionReplyWorkItem.id))).scalars().all())
        executions_after_approval = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())

    assert record_after_approval is not None
    assert record_after_approval.status == AuditRecordStatus.CANCELLED
    assert len(works_after_approval) == 1
    assert works_after_approval[0].id == foreground_work.id
    assert works_after_approval[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert not any(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in works_after_approval)
    assert executions_after_approval == []


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 9])
async def test_stop_during_preparing_audit_discards_delayed_result_without_confirmation_or_reply(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    score: int,
) -> None:
    uid = "owner"
    session_id = "session-confirmation"
    worker_id = "audit-worker"
    tool_call = InternalToolCall(
        id="audit-stop-call",
        name="execute_shell",
        arguments={"command": "echo audit-stop", "execution_mode": "non_interactive"},
    )
    profile_configs = _profile_config()
    profile_configs["security"].update({"audit_channel_id": 1, "audit_model_id": "audit-model"})
    cfg = ProfileConfig.model_validate(profile_configs)

    async with confirmation_workflow_session_factory() as db:
        db.add(Profile(id=1, uid=uid, name="confirmation workflow", configs=profile_configs))
        db.add(ChatSession(session_id=session_id, uid=uid, profile_id=1, source="http", reply_target_source="http"))
        await db.flush()
        user_message = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="run audit-stop",
            is_processed=True,
        )
        source_message = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]).model_dump_json(exclude_none=True),
            is_processed=True,
        )
        db.add_all([user_message, source_message])
        await db.commit()
        assert user_message.id is not None
        assert source_message.id is not None
        user_message_id = user_message.id
        source_message_id = source_message.id

    async with confirmation_workflow_session_factory() as db:
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=user_message_id,
            dedupe_key="audit-stop-foreground",
        )
        assert created
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed is not None
    assert claimed.id == work.id
    assert claimed.work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert claimed.source_type == SessionReplySourceType.USER_MESSAGE
    assert claimed.status == SessionReplyWorkStatus.RUNNING
    assert claimed.id is not None
    work_id = claimed.id

    audit_started = asyncio.Event()
    release_audit = asyncio.Event()

    async def delayed_auditor(db: AsyncSession, *_args, **_kwargs) -> tuple[dict, dict]:
        await db.commit()
        audit_started.set()
        await release_audit.wait()
        return (
            {"messages": []},
            {
                "parsed": {
                    "results": [
                        {
                            "tool_call_id": tool_call.id,
                            "score": score,
                            "reason": "delayed audit",
                            "deny": False,
                            "file_checks": [],
                        }
                    ]
                },
                "file_reads": [],
            },
        )

    async def fail_summary(*_args, **_kwargs):
        raise AssertionError("pending audit summary must not start after stop")

    monkeypatch.setattr(audit_service_module, "_call_auditor", delayed_auditor)
    monkeypatch.setattr(audit_service_module, "_summarize_pending", fail_summary)

    async def run_audit() -> object:
        async with confirmation_workflow_session_factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
            return await audit_service_module.audit_tool_round(
                db,
                cfg=cfg,
                tool_calls=[tool_call],
                source_assistant_message_id=source_message_id,
                uid=uid,
                operator_username=uid,
                session_id=session_id,
                source="http",
                language="zh",
                working_directory=tmp_path,
            )

    audit_task = asyncio.create_task(run_audit())
    try:
        await asyncio.wait_for(audit_started.wait(), timeout=5)
        async with confirmation_workflow_session_factory() as db:
            records = list((await db.execute(select(AuditRecord).order_by(AuditRecord.id))).scalars().all())
            confirmation_cards = list((await db.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        assert len(records) == 1
        preparing_record = records[0]
        assert preparing_record.id is not None
        assert preparing_record.status == AuditRecordStatus.PREPARING
        assert confirmation_cards == []

        stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
        assert stop_payload == {"session_id": session_id, "cancelled_count": 1}
        release_audit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(audit_task, timeout=5)
    finally:
        release_audit.set()
        if not audit_task.done():
            audit_task.cancel()
        try:
            await audit_task
        except asyncio.CancelledError:
            pass

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, preparing_record.id)
        work = await db.get(SessionReplyWorkItem, work_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == preparing_record.id))).scalars().all())
        confirmation_cards = list((await db.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == preparing_record.id))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())

    assert record is not None
    assert record.status == AuditRecordStatus.CANCELLED
    assert record.completed_at is not None
    assert record.execution_claim_token is None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None and work.lock_until is None
    assert work.result_message_id is None
    assert claims == []
    assert confirmation_cards == []
    assert executions == []
    assert len(messages) == 2
    assert {message.id for message in messages} == {user_message_id, source_message_id}
    assert not any(message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "interrupted"])
async def test_confirmation_execution_stop_preserves_started_result_and_cancels_remaining(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None and record.source_assistant_message_id is not None
        source_message_id = record.source_assistant_message_id
        confirmation_card = (await db.execute(select(Message).where(Message.session_id == "session-confirmation", Message.uid == "owner", Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().one()
        assert confirmation_card.id is not None
        confirmation_card_id = confirmation_card.id

    for module in (consumer_module, executor_lifecycle_module, executor_audit_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", confirmation_workflow_session_factory)

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)

    commands: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class ControlledShellExecutor:
        requires_audit = True

        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **arguments) -> str:
            commands.append(arguments["command"])
            assert len(commands) == 1
            first_started.set()
            await release_first.wait()
            if outcome == "interrupted":
                raise AssertionError("interrupted shell call was not cancelled")
            if outcome == "success":
                payload = {"status": "success", "exit_code": 0, "stdout": "controlled success\n"}
            else:
                payload = {
                    "status": "failed",
                    "exit_code": 7,
                    "stdout": "controlled failed\n",
                    "error": "controlled failure",
                }
            return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, "execute_shell", ControlledShellExecutor)

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, submitted_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="approve",
            attachments=None,
            source="http",
        )
    assert submission_status == "approved"
    assert submitted_work.id is not None

    worker_id = "confirmation-stop-worker"
    async with confirmation_workflow_session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed is not None
    assert claimed.id == submitted_work.id
    assert claimed.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION

    consumer = consumer_module.SessionReplyConsumer()
    work_task = asyncio.create_task(consumer._run_claimed(claimed.id, worker_id, claimed.attempt_count, claimed.max_attempts))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=5)
        stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
        assert stop_payload == {"session_id": "session-confirmation", "cancelled_count": 1}

        async with confirmation_workflow_session_factory() as db:
            executing_record = await db.get(AuditRecord, audit_record_id)
        assert executing_record is not None
        assert executing_record.status == AuditRecordStatus.EXECUTING

        if outcome == "interrupted":
            work_task.cancel()
        else:
            release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work_task, timeout=5)
    finally:
        release_first.set()
        if not work_task.done():
            work_task.cancel()
        await asyncio.gather(work_task, return_exceptions=True)

    expected_first_execution_status = {
        "success": AuditExecutionStatus.SUCCEEDED,
        "failed": AuditExecutionStatus.FAILED,
        "interrupted": AuditExecutionStatus.EXECUTION_UNKNOWN,
    }[outcome]
    expected_audit_status = AuditRecordStatus.EXECUTION_UNKNOWN if outcome == "interrupted" else AuditRecordStatus.CANCELLED
    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        work = await db.get(SessionReplyWorkItem, submitted_work.id)
        source_message = await db.get(Message, source_message_id)
        card = await db.get(Message, confirmation_card_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == audit_record_id))).scalars().all())
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())

    assert commands == ["echo confirmed-1"]
    assert record is not None
    assert record.status == expected_audit_status
    assert record.decision is not None and record.decision.value == "approve"
    assert record.decision_raw_message == "approve"
    assert record.execution_claim_token is None
    assert record.completed_at is not None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None and work.lock_until is None
    assert work.result_message_id is None
    assert [execution.status for execution in executions] == [expected_first_execution_status, AuditExecutionStatus.CANCELLED]
    assert all(execution.status != AuditExecutionStatus.RUNNING for execution in executions)
    assert claims == []
    assert source_message is not None and source_message.id == source_message_id and source_message.type == MessageType.TOOL_CALL
    assert card is not None and card.id == confirmation_card_id
    assert json.loads(card.content or "{}")["status"] == expected_audit_status.value

    structured_results = [message for message in messages if message.audit_record_id == audit_record_id and message.audit_tool_call_id is not None and message.type == MessageType.TOOL_RESULT]
    assert [message.audit_tool_call_id for message in structured_results] == [
        "original-shell-call-1",
        "original-shell-call-2",
    ]
    result_payloads = {message.audit_tool_call_id: json.loads(InternalMessage.model_validate_json(message.content or "{}").content or "{}") for message in structured_results}
    first_payload = result_payloads["original-shell-call-1"]
    assert first_payload["confirmation_decision"] == "approve"
    if outcome == "success":
        assert first_payload["status"] == "success"
        assert first_payload["exit_code"] == 0
        assert first_payload["stdout"] == "controlled success\n"
    elif outcome == "failed":
        assert first_payload["status"] == "failed"
        assert first_payload["exit_code"] == 7
        assert first_payload["stdout"] == "controlled failed\n"
        assert first_payload["error"] == "controlled failure"
    else:
        assert first_payload["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
        assert first_payload["confirmation_status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
        assert first_payload["error"]
    second_payload = result_payloads["original-shell-call-2"]
    assert second_payload["status"] == AuditRecordStatus.CANCELLED.value
    assert second_payload["confirmation_status"] == AuditRecordStatus.CANCELLED.value
    assert second_payload["confirmation_decision"] == "approve"
    assert second_payload["error"]

    versions_by_call: dict[str, list[AuditToolResultVersion]] = {}
    for version in versions:
        versions_by_call.setdefault(version.original_tool_call_id, []).append(version)
    assert set(versions_by_call) == {"original-shell-call-1", "original-shell-call-2"}
    assert all([version.version_no for version in call_versions] == [0, 1, 2] for call_versions in versions_by_call.values())
    assert len([message for message in messages if message.type == MessageType.AUDIT_CONFIRMATION]) == 1
    assert not any(message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT for message in messages)
    assert not any(message.role == MessageRole.ERR for message in messages)
    assert not any(event.get("source") == "confirmed_tool_execution" for event in delivered_events)


@pytest.mark.asyncio
async def test_stopped_foreground_work_blocks_audit_before_preparation(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uid = "owner"
    session_id = "session-confirmation"
    worker_id = "stopped-foreground-worker"
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None and record.source_assistant_message_id is not None
        source_message_id = record.source_assistant_message_id
        source_message = await db.get(Message, source_message_id)
        assert source_message is not None
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
        assert source_internal.tool_calls
        source_tool_calls = source_internal.tool_calls
        confirmation_card = (await db.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().one()
        assert confirmation_card.id is not None
        confirmation_card_id = confirmation_card.id
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, submitted_work, _submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid=uid,
            session_id=session_id,
            profile=profile,
            message="new operation",
            attachments=None,
            source="http",
        )
        assert submitted_work.id is not None
        assert submitted_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
        assert submitted_work.source_type == SessionReplySourceType.USER_MESSAGE
        assert submitted_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        work_id = submitted_work.id

    async with confirmation_workflow_session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed is not None and claimed.id == work_id
        assert claimed.work_type == SessionReplyWorkType.FOREGROUND_REPLY
        assert claimed.status == SessionReplyWorkStatus.RUNNING

    stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
    assert stop_payload == {"session_id": session_id, "cancelled_count": 1}

    async with confirmation_workflow_session_factory() as db:
        stopped_work = await db.get(SessionReplyWorkItem, work_id)
        assert stopped_work is not None and stopped_work.status == SessionReplyWorkStatus.CANCELLED

    profile_configs = _profile_config()
    profile_configs["security"].update({"audit_channel_id": 1, "audit_model_id": "audit-model"})
    cfg = ProfileConfig.model_validate(profile_configs)

    async def fail_call_auditor(*_args, **_kwargs):
        raise AssertionError("stopped work must not call the auditor")

    monkeypatch.setattr(audit_service_module, "_call_auditor", fail_call_auditor)

    async with confirmation_workflow_session_factory() as db:
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        with pytest.raises(asyncio.CancelledError):
            await audit_service_module.audit_tool_round(
                db,
                cfg=cfg,
                tool_calls=source_tool_calls,
                source_assistant_message_id=source_message_id,
                uid=uid,
                operator_username=uid,
                session_id=session_id,
                source="http",
                language="zh",
                working_directory=tmp_path,
            )

    async with confirmation_workflow_session_factory() as db:
        records = (await db.execute(select(AuditRecord).order_by(AuditRecord.id))).scalars().all()
        confirmation_cards = (await db.execute(select(Message).where(Message.session_id == session_id, Message.uid == uid, Message.type == MessageType.AUDIT_CONFIRMATION).order_by(Message.id))).scalars().all()
        executions = (await db.execute(select(AuditExecutionRecord))).scalars().all()
        work = await db.get(SessionReplyWorkItem, work_id)

    assert len(records) == 1
    assert records[0].id == audit_record_id
    assert records[0].status == AuditRecordStatus.CANCELLED
    assert records[0].source_assistant_message_id == source_message_id
    assert len(confirmation_cards) == 1
    assert confirmation_cards[0].id == confirmation_card_id
    assert executions == []
    assert work is not None and work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None and work.lock_until is None
    assert delivered_events


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_requested", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("decision", ["同意", "忽略", "拒绝"], ids=["approve", "ignore", "reject"])
async def test_confirmation_audit_decision_continues_without_forced_memory_recall(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_requested: bool,
    decision: str,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        session = await db.get(ChatSession, "session-confirmation")
        assert profile is not None
        assert session is not None
        profile.configs = _profile_config()
        profile.configs["memory"].update({"enabled": True, "precheck_enabled": True})
        session.max_turns = 3
        if decision == "忽略":
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
            assert details
            for detail in details:
                detail.score = 8
        await db.commit()

    for module in (executor_lifecycle_module, executor_interactive_module, executor_metadata_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", confirmation_workflow_session_factory)

    fake_channel = SimpleNamespace(
        id=1,
        base_url="https://chat.invalid",
        get_decrypted_api_key=lambda: "test-key",
        http_proxy=None,
    )
    fake_model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 256,
    }
    fake_channel_rule = SimpleNamespace(channel_id=1, model_id="chat-model", priority=1, weight=1)

    async def fake_select_channel(_db, _channel_cfg, expected_usage: str, **_kwargs):
        assert expected_usage == "CHAT"
        return fake_channel, dict(fake_model_entry), fake_channel_rule

    for module in (
        dispatcher_module,
        interactive_runtime_module,
        interactive_generation_module,
        memory_request_module,
        channel_call_module,
    ):
        monkeypatch.setattr(module, "select_channel", fake_select_channel)

    commands: list[str] = []

    class FakeShellExecutor:
        requires_audit = True

        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **arguments) -> str:
            command = arguments["command"]
            commands.append(command)
            return json.dumps(
                {
                    "status": "success",
                    "command": command,
                    "stdout": f"{command}\n",
                    "stderr": "",
                    "exit_code": 0,
                },
                ensure_ascii=False,
            )

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, "execute_shell", FakeShellExecutor)

    confirmation_events: list[dict] = []
    executor_lifecycle_events: list[dict] = []
    executor_interactive_events: list[dict] = []

    async def capture_confirmation_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        confirmation_events.append(event_payload)

    async def capture_lifecycle_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        executor_lifecycle_events.append(event_payload)

    async def capture_interactive_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        executor_interactive_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_confirmation_event)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_lifecycle_event)
    monkeypatch.setattr(executor_interactive_module, "send_session_stream_event", capture_interactive_event)

    captured_requests: list[dict[str, object]] = []
    forced_recall_requests: list[list[InternalMessage]] = []
    normal_turn = 0

    def build_fake_llm_response(messages: list[InternalMessage], tools) -> InternalResponse:
        nonlocal normal_turn
        request_messages = [message.model_copy(deep=True) for message in messages]
        captured_requests.append({"messages": request_messages, "tools": tools})
        first_system_content = next(
            (message.content for message in request_messages if message.role == MessageRole.SYSTEM),
            None,
        )
        if first_system_content == LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT:
            forced_recall_requests.append(request_messages)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="unexpected forced recall"),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                finish_reason="stop",
            )

        if decision == "拒绝":
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="rejected reply"),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                finish_reason="stop",
            )

        if normal_turn == 0:
            assert tools
            assert any(item.get("function", {}).get("name") == MANAGE_TODO_TOOL_NAME for item in tools)
            normal_turn = 1
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="todo-call",
                            name=MANAGE_TODO_TOOL_NAME,
                            arguments={"operation": "read"},
                        )
                    ],
                ),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                finish_reason="tool_calls",
            )

        normal_turn += 1
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="continued reply"),
            model="chat-model",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            finish_reason="stop",
        )

    async def fake_generate(**kwargs) -> InternalResponse:
        return build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))

    async def fake_generate_with_stream_callback(**kwargs) -> InternalResponse:
        response = build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))
        if response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(LLMClient, "generate", fake_generate)
    monkeypatch.setattr(LLMClient, "generate_with_stream_callback", fake_generate_with_stream_callback)

    source = "ws" if stream_requested else "http"
    worker_id = f"confirmation-audit-reply-{source}-{decision}"
    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, submitted_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message=decision,
            attachments=None,
            source=source,
            stream_requested=stream_requested,
            actor_username="owner",
        )
        assert submitted_work.id is not None
        assert submission_status in {"approved", "rejected"}
        submitted_work_id = submitted_work.id

    async with confirmation_workflow_session_factory() as db:
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed_work is not None
    assert claimed_work.id == submitted_work_id
    await executor_lifecycle_module.execute_session_reply_work(claimed_work.id, worker_id)

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        work = await db.get(SessionReplyWorkItem, submitted_work_id)
        decision_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.AUDIT_DECISION,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())

    assert record is not None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.SUCCEEDED
    assert work.work_type == (SessionReplyWorkType.FOREGROUND_REPLY if decision == "拒绝" else SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION)
    assert len(decision_messages) == 1
    decision_message = decision_messages[0]
    assert decision_message.type == MessageType.AUDIT_DECISION
    assert decision_message.content == decision
    assert record.decision_message_id == decision_message.id
    assert record.decision_raw_message == decision
    expected_decision = {"同意": "approve", "忽略": "approve", "拒绝": "reject"}[decision]
    expected_record_status = AuditRecordStatus.REJECTED if decision == "拒绝" else AuditRecordStatus.SUCCEEDED
    assert record.decision is not None and record.decision.value == expected_decision
    assert record.status == expected_record_status
    assert forced_recall_requests == []
    assert not [message for message in messages if (message.dedupe_key or "").startswith("memory-recall-")]

    checkpoint = (work.execution_state or {}).get("dispatcher_checkpoint") or {}
    assert checkpoint.get("memory_recall_boundary_message_id") != decision_message.id
    final_message = next(message for message in messages if message.id == work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.type == MessageType.TEXT
    assert final_message.content == ("rejected reply" if decision == "拒绝" else "continued reply")

    if decision == "拒绝":
        assert commands == []
        assert executions == []
        assert not any(json.loads(InternalMessage.model_validate_json(message.content or "{}").content or "{}").get("operation") == "read" for message in messages if message.type == MessageType.TOOL_RESULT)
        assert captured_requests[0]["tools"] is None
        assert len(captured_requests) == 1
    else:
        assert commands == ["echo confirmed-1", "echo confirmed-2"]
        assert [execution.status for execution in executions] == [
            AuditExecutionStatus.SUCCEEDED,
            AuditExecutionStatus.SUCCEEDED,
        ]
        todo_results = []
        for message in messages:
            if message.type != MessageType.TOOL_RESULT:
                continue
            internal = InternalMessage.model_validate_json(message.content or "{}")
            if internal.tool_call_id != "todo-call":
                continue
            todo_results.append(json.loads(internal.content or "{}"))
        assert todo_results
        assert todo_results[0] == {
            "status": "success",
            "operation": "read",
            "revision": 0,
            "todos": [],
        }
        assert len(captured_requests) == 2

    assert executor_lifecycle_events
    if stream_requested and decision != "拒绝":
        assert executor_interactive_events
