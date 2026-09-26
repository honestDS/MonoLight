import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.providers.database as database_provider
from app.adapters.chat_web import web_chat_adapter
from app.core.audit import confirmation_events as confirmation_events_module
from app.core.audit.confirmation_persistence import persist_pending_confirmation_bundle
from app.core.audit.integrity import build_tool_round_integrity_snapshot
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import manager_result as manager_result_module
from app.core.session_reply_queue import manager_submission as manager_submission_module
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.utils.dispatcher.save_message import save_message
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
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseProfileBinding
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from tests.database_support import clone_sqlite_schema


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
        PromptLibrary.__table__,
        Profile.__table__,
        ChatSession.__table__,
        SessionTodoPlan.__table__,
        KnowledgeBase.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        Message.__table__,
        AuditRecord.__table__,
        AuditToolDetail.__table__,
        AuditConfirmationClaim.__table__,
        AuditExecutionRecord.__table__,
        AuditToolResultVersion.__table__,
        SessionReplySequence.__table__,
        SessionReplyWorkItem.__table__,
    ]
    await clone_sqlite_schema(tmp_path / "confirmation-workflow.db", tables=tables)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
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
    integrity = build_tool_round_integrity_snapshot(
        tool_calls=[{"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments} for tool_call in tool_calls],
        uid=uid,
        session_id=session_id,
        working_directory=working_directory,
    )
    async with session_factory() as db:
        profile = Profile(id=1, uid=uid, name="confirmation workflow", configs=_profile_config())
        db.add(profile)
        db.add(ChatSession(session_id=session_id, uid=uid, profile_id=1))
        await db.flush()

        source = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls).model_dump_json(exclude_none=True),
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


@pytest.mark.asyncio
async def test_confirmation_workflow_approves_executes_replaces_pending_result_and_completes_reply(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    monkeypatch.setattr(database_provider, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
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

    async def continue_reply(
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        **kwargs,
    ) -> dict:
        result = await db.execute(select(Message).where(Message.session_id == work.session_id).order_by(Message.id))
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

        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="confirmed tool completed"),
            work.profile_id,
            dedupe_key=_result_message_dedupe_key(work),
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
        }

    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", continue_reply)

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

    assert record is not None
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
    final_message = next(message for message in messages if message.id == work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.content == "confirmed tool completed"
    assert any(event.get("source") == "confirmed_tool_execution" for event in delivered_events)


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
