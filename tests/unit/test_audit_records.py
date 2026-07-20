import asyncio
import json
import os
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.audit.confirmation import ConfirmationDecision, cancel_confirmation_by_session, notify_confirmation_tool_results, update_confirmation_message_status, update_confirmation_tool_results_for_decision
from app.core.audit.integrity import build_tool_round_integrity_snapshot, summarize_tool_arguments, verify_persisted_tool_round, verify_tool_round_integrity
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.audit.startup import recover_and_cleanup_audit_data
from app.core.audit.storage import AuditCleanupResult
from app.core.constants import ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER
from app.core.crud.audit import audit_crud
from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditFailureType,
    AuditRecord,
    AuditRecordStatus,
)
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType


@pytest.fixture
async def audit_database(tmp_path):
    database_path = tmp_path / "audit.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _tool_calls():
    return [
        {"id": "call-1", "name": "execute_shell", "arguments": {"command": "python entry.py"}},
        {"id": "call-2", "name": "write_file", "arguments": {"file_path": "result.txt", "content": "value"}},
    ]


def _tool_details(round_snapshot, conclusion="pending"):
    return [
        {
            "original_tool_call_id": item.tool_call_id,
            "turn_index": item.turn_index,
            "tool_name": item.tool_name,
            "conclusion": conclusion,
            "score": 5 if conclusion == "pending" else 1,
            "reason": "test",
            "arguments_hash": item.arguments_sha256,
            "arguments_summary": summarize_tool_arguments(item.arguments),
            "file_snapshots": [],
        }
        for item in round_snapshot.tool_calls
    ]


async def _create_preparing(session, tmp_path, tool_count=2):
    snapshot = build_tool_round_integrity_snapshot(
        tool_calls=_tool_calls()[:tool_count],
        uid="u1",
        session_id="session-1",
        working_directory=tmp_path,
    )
    record = await audit_crud.create_preparing(
        session,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="zh",
        source_assistant_message_id=10,
        working_directory=str(tmp_path),
        round_arguments_hash=snapshot.round_sha256,
        tool_count=tool_count,
    )
    return record, snapshot


async def _add_pending_confirmation_messages(session, record, *, include_card=True):
    source_message = Message(
        uid=record.uid,
        session_id=record.session_id,
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "python entry.py"})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    session.add(source_message)
    await session.flush()
    record.source_assistant_message_id = source_message.id
    tool_result = Message(
        uid=record.uid,
        session_id=record.session_id,
        profile_id=1,
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        content=InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-1",
            content=json.dumps({"status": AuditRecordStatus.PENDING.value, "error": "waiting"}),
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    session.add(tool_result)
    card = None
    if include_card:
        card = Message(
            uid=record.uid,
            session_id=record.session_id,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
            is_processed=True,
        )
        session.add(card)
    await session.commit()
    return tool_result, card


def test_round_integrity_binds_order_identity_and_arguments(tmp_path):
    snapshot = build_tool_round_integrity_snapshot(
        tool_calls=_tool_calls(),
        uid="u1",
        session_id="session-1",
        working_directory=tmp_path,
    )

    assert verify_tool_round_integrity(snapshot, tool_calls=_tool_calls(), uid="u1", session_id="session-1", working_directory=tmp_path)
    assert not verify_tool_round_integrity(snapshot, tool_calls=list(reversed(_tool_calls())), uid="u1", session_id="session-1", working_directory=tmp_path)
    assert not verify_tool_round_integrity(snapshot, tool_calls=_tool_calls(), uid="u2", session_id="session-1", working_directory=tmp_path)
    changed = _tool_calls()
    changed[0]["arguments"] = {"command": "python changed.py"}
    assert not verify_tool_round_integrity(snapshot, tool_calls=changed, uid="u1", session_id="session-1", working_directory=tmp_path)
    assert "python entry.py" not in summarize_tool_arguments(_tool_calls()[0]["arguments"])
    persisted_calls = [
        {
            "original_tool_call_id": item.tool_call_id,
            "turn_index": item.turn_index,
            "tool_name": item.tool_name,
            "arguments_hash": item.arguments_sha256,
        }
        for item in snapshot.tool_calls
    ]
    assert verify_persisted_tool_round(
        expected_round_sha256=snapshot.round_sha256,
        expected_tool_calls=persisted_calls,
        tool_calls=_tool_calls(),
        uid="u1",
        session_id="session-1",
        working_directory=tmp_path,
    )
    persisted_calls[0]["arguments_hash"] = "0" * 64
    assert not verify_persisted_tool_round(
        expected_round_sha256=snapshot.round_sha256,
        expected_tool_calls=persisted_calls,
        tool_calls=_tool_calls(),
        uid="u1",
        session_id="session-1",
        working_directory=tmp_path,
    )


def test_round_integrity_rejects_duplicate_call_ids(tmp_path):
    duplicated = _tool_calls()
    duplicated[1]["id"] = duplicated[0]["id"]

    with pytest.raises(ValueError, match="缺失或重复"):
        build_tool_round_integrity_snapshot(
            tool_calls=duplicated,
            uid="u1",
            session_id="session-1",
            working_directory=tmp_path,
        )


@pytest.mark.asyncio
async def test_pending_round_is_claimed_only_once_and_records_each_execution(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path)
        context_file = (tmp_path / "audit_1.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        tool_details = _tool_details(snapshot)
        tool_details[0]["file_snapshots"] = [
            {
                "original_path": "entry.py",
                "absolute_path": str((tmp_path / "entry.py").resolve()),
                "sha256": "a" * 64,
                "size": 10,
                "content": "must not enter database",
            }
        ]
        completed = await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=tool_details,
            context_file_path=str(context_file),
            intent_summary="执行命令并写入文件",
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        assert completed
        current = await audit_crud.get_current_confirmation(session, uid="u1", session_id="session-1")
        assert current is not None and current.id == record.id

    async def claim(message_id):
        async with audit_database() as claim_session:
            return await audit_crud.claim_pending_for_execution(
                claim_session,
                audit_record_id=record.id,
                uid="u1",
                session_id="session-1",
                decision_message_id=message_id,
                decision_raw_message="同意",
                decided_by="tester",
            )

    claims = await asyncio.gather(claim(20), claim(21))
    successful_claims = [(claimed, token) for claimed, token in claims if claimed is not None]
    assert len(successful_claims) == 1
    claimed_record, claim_token = successful_claims[0]
    assert claimed_record.status == AuditRecordStatus.EXECUTING
    assert claim_token

    async with audit_database() as session:
        details = await audit_crud.list_tool_details(session, record.id)
        assert "content" not in details[0].file_snapshots[0]
        executions = []
        for index, detail in enumerate(details):
            execution = await audit_crud.create_execution_attempt(
                session,
                audit_record_id=record.id,
                audit_tool_detail_id=detail.id,
                claim_token=claim_token,
                execution_node="node-1",
                new_tool_call_id=f"new-call-{index}",
            )
            assert execution is not None
            executions.append(execution)
            assert await audit_crud.finish_execution_attempt(
                session,
                execution_record_id=execution.id,
                status=AuditExecutionStatus.SUCCEEDED,
                result_summary="success",
            )

        assert await audit_crud.finish_execution_round(
            session,
            audit_record_id=record.id,
            claim_token=claim_token,
            status=AuditRecordStatus.SUCCEEDED,
        )
        stored_record = await audit_crud.get_record(session, record.id)
        assert stored_record.status == AuditRecordStatus.SUCCEEDED
        execution_result = await session.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == record.id))
        assert len(execution_result.scalars().all()) == 2
        claim_result = await session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))
        assert claim_result.scalars().first() is None


@pytest.mark.asyncio
async def test_preparation_status_is_claimed_before_related_rows_are_written(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_prepare_once.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        details = _tool_details(snapshot)

        assert await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=details,
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        assert not await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=details,
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )

        stored_details = await audit_crud.list_tool_details(session, record.id)
        claim_result = await session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))
        assert len(stored_details) == 1
        assert len(claim_result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_source_message_failure_uses_distinct_failure_type(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_source.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        _, token = await audit_crud.claim_pending_for_execution(
            session,
            audit_record_id=record.id,
            uid="u1",
            session_id="session-1",
            decision_message_id=20,
            decision_raw_message="同意",
            decided_by="tester",
        )
        assert token
        assert await audit_crud.mark_source_message_invalid(session, audit_record_id=record.id, claim_token=token, error_reason="参数摘要不匹配")
        stored = await audit_crud.get_record(session, record.id)
        assert stored.status == AuditRecordStatus.FAILED
        assert stored.failure_type == AuditFailureType.SOURCE_MESSAGE_INVALID


@pytest.mark.asyncio
async def test_changed_file_cancels_execution_claim_for_reaudit(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_changed_file.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        _, token = await audit_crud.claim_pending_for_execution(
            session,
            audit_record_id=record.id,
            uid="u1",
            session_id="session-1",
            decision_message_id=20,
            decision_raw_message="同意",
            decided_by="tester",
        )

        assert token
        assert await audit_crud.cancel_execution_for_file_reaudit(
            session,
            audit_record_id=record.id,
            claim_token=token,
            error_reason="file changed",
        )
        stored = await audit_crud.get_record(session, record.id)
        assert stored.status == AuditRecordStatus.CANCELLED
        assert stored.execution_claim_token is None


@pytest.mark.asyncio
async def test_persistence_failure_can_finish_without_context_file(audit_database, tmp_path):
    async with audit_database() as session:
        record, _ = await _create_preparing(session, tmp_path, tool_count=1)
        record_id = record.id
        assert await audit_crud.mark_persistence_failed(session, audit_record_id=record_id, error_reason="file write failed")
        stored = await audit_crud.get_record(session, record_id)
        assert stored.status == AuditRecordStatus.AUDIT_FAILED
        assert stored.failure_type == AuditFailureType.AUDIT_PERSISTENCE_FAILED
        assert stored.context_file_path is None


@pytest.mark.asyncio
async def test_persistence_service_marks_file_write_failure(audit_database, tmp_path, monkeypatch):
    from app.core.audit import persistence as persistence_module

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record_id = record.id

        async def fail_write(**kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(persistence_module, "write_audit_json", fail_write)
        persisted = await persist_prepared_audit_round(
            session,
            audit_record_id=record_id,
            uid="u1",
            status=AuditRecordStatus.PASSED,
            context_payload={"tools": _tool_calls()[:1]},
            tool_details=_tool_details(snapshot, conclusion="passed"),
            audit_root=tmp_path / "audit",
        )

        assert not persisted
        stored = await audit_crud.get_record(session, record_id)
        assert stored.status == AuditRecordStatus.AUDIT_FAILED
        assert stored.failure_type == AuditFailureType.AUDIT_PERSISTENCE_FAILED
        assert stored.context_file_path is None


@pytest.mark.asyncio
async def test_session_cancellation_removes_only_current_claim(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_cancel.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        assert await cancel_confirmation_by_session(session, uid="u1", session_id="session-1", locale="en") == 1
        stored = await audit_crud.get_record(session, record.id)
        assert stored.status == AuditRecordStatus.CANCELLED
        assert stored.error_reason == "The pending audit was superseded by a new tool call"
        assert await audit_crud.get_current_confirmation(session, uid="u1", session_id="session-1") is None


@pytest.mark.asyncio
async def test_expired_confirmation_is_closed_and_cannot_be_claimed(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record_id = record.id
        context_file = (tmp_path / "audit_expired.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() - timedelta(seconds=1),
        )

        assert await audit_crud.get_current_confirmation(session, uid="u1", session_id="session-1") is None
        assert await audit_crud.expire_pending_confirmations(session) == 1
        claimed, token = await audit_crud.claim_pending_for_execution(
            session,
            audit_record_id=record_id,
            uid="u1",
            session_id="session-1",
            decision_message_id=20,
            decision_raw_message="同意",
            decided_by="tester",
        )
        assert claimed is None and token is None
        stored = await audit_crud.get_record(session, record_id)
        assert stored.status == AuditRecordStatus.EXPIRED


@pytest.mark.asyncio
async def test_session_expiration_expires_record_and_removes_claim(audit_database, tmp_path):
    async with audit_database() as session:
        assert await audit_crud.expire_confirmation_by_session(session, uid="missing", session_id="missing") == 0

        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_session_expired.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() - timedelta(seconds=1),
        )

        assert await audit_crud.expire_confirmation_by_session(session, uid="u1", session_id="session-1") == 1
        stored = await audit_crud.get_record(session, record.id)
        claim_result = await session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))
        assert stored.status == AuditRecordStatus.EXPIRED
        assert claim_result.scalars().first() is None


@pytest.mark.asyncio
async def test_confirmation_status_sync_uses_database_status_and_deduplicates_broadcast(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append((uid, session_id, event))

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_confirmation_status.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps(
                {
                    "type": "audit_confirmation",
                    "audit_record_id": record.id,
                    "summary": "run command",
                    "status": AuditRecordStatus.PENDING.value,
                }
            ),
            is_processed=True,
        )
        session.add(card)
        await session.commit()

        await audit_crud.close_pending(
            session,
            audit_record_id=record.id,
            uid="u1",
            session_id="session-1",
            status=AuditRecordStatus.REJECTED,
        )
        assert await update_confirmation_message_status(session, audit_record_id=record.id)
        assert not await update_confirmation_message_status(session, audit_record_id=record.id)
        await session.refresh(card)

        payload = json.loads(card.content)
        assert payload["status"] == AuditRecordStatus.REJECTED.value
    assert [event[2]["status"] for event in events] == [AuditRecordStatus.REJECTED.value]


@pytest.mark.asyncio
async def test_confirmation_status_event_contains_persisted_tool_result_and_is_json_serializable(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append((uid, session_id, event))

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_confirmation_tool_result_event.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot, conclusion="pending")[:1],
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        tool_result, _card = await _add_pending_confirmation_messages(session, record, include_card=False)
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
            is_processed=True,
        )
        session.add(card)
        await session.commit()

        await audit_crud.close_pending(
            session,
            audit_record_id=record.id,
            uid="u1",
            session_id="session-1",
            status=AuditRecordStatus.REJECTED,
        )
        await update_confirmation_tool_results_for_decision(
            session,
            audit_record_id=record.id,
            before_message_id=tool_result.id + 1,
            decision=ConfirmationDecision.REJECT,
            raw_message="拒绝",
        )
        assert await update_confirmation_message_status(session, audit_record_id=record.id)
        await session.refresh(tool_result)

    assert len(events) == 2
    status_events = [item[2] for item in events if item[2]["type"] == "audit_confirmation_status"]
    result_events = [item[2] for item in events if item[2]["type"] == "audit_tool_results_update"]
    assert len(status_events) == 1
    assert len(result_events) == 1
    assert "tool_results" not in status_events[0]
    event = result_events[0]
    json.dumps(event, ensure_ascii=False)
    assert event["messages"] == [
        {
            "id": tool_result.id,
            "db_id": tool_result.id,
            "role": "tool",
            "type": "tool_result",
            "content": tool_result.content,
            "tool_call_id": "call-1",
            "created_at": tool_result.created_at.timestamp(),
        }
    ]
    result_payload = json.loads(InternalMessage.model_validate_json(event["messages"][0]["content"]).content)
    assert result_payload["status"] == AuditRecordStatus.REJECTED.value
    assert result_payload["confirmation_decision"] == "拒绝"
    assert result_payload["rejection_source"] == "user"
    assert result_payload["error"] == t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER, locale="zh")


@pytest.mark.asyncio
async def test_tool_result_notification_reads_result_after_status_event(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(_uid, _session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_confirmation_tool_result_notification.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot)[:1],
            context_file_path=str(context_file),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        tool_result, card = await _add_pending_confirmation_messages(session, record)
        record.status = AuditRecordStatus.REJECTED
        card_payload = {"audit_record_id": record.id, "status": AuditRecordStatus.REJECTED.value}
        await message_crud.update_content(session, message_id=card.id, content=json.dumps(card_payload))
        await message_crud.update_content(
            session,
            message_id=tool_result.id,
            content=InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": AuditRecordStatus.REJECTED.value}),
            ).model_dump_json(exclude_none=True),
        )

        assert await notify_confirmation_tool_results(session, audit_record_id=record.id)

    assert len(events) == 1
    assert events[0]["type"] == "audit_tool_results_update"
    assert events[0]["event_id"].startswith(f"audit-tool-results:{record.id}:")
    payload = json.loads(InternalMessage.model_validate_json(events[0]["messages"][0]["content"]).content)
    assert payload["status"] == AuditRecordStatus.REJECTED.value


@pytest.mark.parametrize(
    "terminal_status",
    [
        AuditRecordStatus.CANCELLED,
        AuditRecordStatus.SUCCEEDED,
        AuditRecordStatus.FAILED,
        AuditRecordStatus.EXECUTION_UNKNOWN,
    ],
)
@pytest.mark.asyncio
async def test_confirmation_status_sync_covers_execution_terminal_states(audit_database, tmp_path, monkeypatch, terminal_status):
    events = []

    async def send_event(uid, session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

    async with audit_database() as session:
        record, _snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record.status = terminal_status
        await session.commit()
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
            is_processed=True,
        )
        session.add(card)
        await session.commit()

        assert await update_confirmation_message_status(session, audit_record_id=record.id)
        await session.refresh(card)

        assert json.loads(card.content)["status"] == terminal_status.value
        assert [event["status"] for event in events] == [terminal_status.value]


@pytest.mark.asyncio
async def test_confirmation_status_sync_uses_database_compare_and_swap_across_sessions(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

    async with audit_database() as session:
        record, _snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record.status = AuditRecordStatus.SUCCEEDED
        await session.commit()
        session.add(
            Message(
                uid="u1",
                session_id="session-1",
                profile_id=1,
                role=MessageRole.ASSISTANT,
                type=MessageType.AUDIT_CONFIRMATION,
                content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
                is_processed=True,
            )
        )
        await session.commit()

    async def sync_from_worker():
        async with audit_database() as session:
            return await update_confirmation_message_status(session, audit_record_id=record.id)

    results = await asyncio.gather(sync_from_worker(), sync_from_worker())

    async with audit_database() as session:
        card = (
            (
                await session.execute(
                    select(Message).where(
                        Message.uid == "u1",
                        Message.session_id == "session-1",
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .one()
        )

    assert sum(results) == 1
    assert json.loads(card.content)["status"] == AuditRecordStatus.SUCCEEDED.value
    assert [event["status"] for event in events] == [AuditRecordStatus.SUCCEEDED.value]


@pytest.mark.asyncio
async def test_confirmation_status_sync_reloads_database_status_after_compare_and_swap_conflict(audit_database, tmp_path, monkeypatch):
    events = []
    first_attempt = True

    async def send_event(_uid, _session_id, event):
        events.append(event)

    async with audit_database() as session:
        record, _snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record.status = AuditRecordStatus.EXECUTING
        await session.commit()
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
            is_processed=True,
        )
        session.add(card)
        await session.commit()

        original_update = message_crud.update_content_if_matches

        async def conflicting_update(db, **kwargs):
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                await db.execute(update(AuditRecord).where(AuditRecord.id == record.id).values(status=AuditRecordStatus.SUCCEEDED))
                await db.execute(update(Message).where(Message.id == card.id).values(content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.SUCCEEDED.value})))
                await db.commit()
                return False
            return await original_update(db, **kwargs)

        monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)
        monkeypatch.setattr("app.core.audit.confirmation.message_crud.update_content_if_matches", conflicting_update)

        assert not await update_confirmation_message_status(session, audit_record_id=record.id)
        await session.refresh(card)

        assert json.loads(card.content)["status"] == AuditRecordStatus.SUCCEEDED.value
        assert events == []


@pytest.mark.asyncio
async def test_startup_expiration_syncs_confirmation_card_from_final_record(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)
    audit_root = tmp_path / "audit"

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = audit_root / "temp_u1" / "audit_1.json"
        context_file.parent.mkdir(parents=True)
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file.resolve()),
            expires_at=get_local_time() - timedelta(seconds=1),
        )
        tool_result, card = await _add_pending_confirmation_messages(session, record)
        assert card is not None

        await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
        await session.refresh(card)
        await session.refresh(tool_result)
        stored_record = await audit_crud.get_record(session, record.id)
        tool_result_payload = json.loads(InternalMessage.model_validate_json(tool_result.content).content)

        assert stored_record.status == AuditRecordStatus.EXPIRED
        assert json.loads(card.content)["status"] == AuditRecordStatus.EXPIRED.value
        assert tool_result_payload["status"] == AuditRecordStatus.EXPIRED.value
        assert tool_result_payload["confirmation_status"] == AuditRecordStatus.EXPIRED.value
        assert "安全审计确认已过期" in tool_result_payload["error"]
        status_events = [event for event in events if event["type"] == "audit_confirmation_status"]
        result_events = [event for event in events if event["type"] == "audit_tool_results_update"]
        assert [event["status"] for event in status_events] == [AuditRecordStatus.EXPIRED.value]
        assert len(result_events) == 1


@pytest.mark.asyncio
async def test_startup_expiration_commits_tool_result_without_confirmation_card(audit_database, tmp_path):
    audit_root = tmp_path / "audit"
    context_file = audit_root / "temp_u1" / "audit_no_card.json"
    context_file.parent.mkdir(parents=True)
    context_file.write_text("{}", encoding="utf-8")

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        record.language = "en"
        await session.commit()
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(context_file.resolve()),
            expires_at=get_local_time() - timedelta(seconds=1),
        )
        tool_result, card = await _add_pending_confirmation_messages(session, record, include_card=False)
        record_id = record.id
        tool_result_id = tool_result.id
        assert card is None

        await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)

    async with audit_database() as session:
        stored_record = await audit_crud.get_record(session, record_id)
        stored_tool_result = await session.get(Message, tool_result_id)
        assert stored_record.status == AuditRecordStatus.EXPIRED
        assert stored_tool_result is not None
        tool_result_payload = json.loads(InternalMessage.model_validate_json(stored_tool_result.content).content)
        assert tool_result_payload["status"] == AuditRecordStatus.EXPIRED.value
        assert tool_result_payload["confirmation_status"] == AuditRecordStatus.EXPIRED.value
        assert tool_result_payload["error"].startswith("The security-audit confirmation expired")


@pytest.mark.asyncio
async def test_startup_marks_interrupted_execution_unknown(audit_database, tmp_path):
    audit_root = tmp_path / "audit"
    audit_file = audit_root / "temp_u1" / "audit_1.json"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text("{}", encoding="utf-8")

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PASSED,
            tool_details=_tool_details(snapshot, conclusion="passed"),
            context_file_path=str(audit_file.resolve()),
        )
        _, token = await audit_crud.claim_passed_for_execution(session, audit_record_id=record.id)
        detail = (await audit_crud.list_tool_details(session, record.id))[0]
        execution = await audit_crud.create_execution_attempt(
            session,
            audit_record_id=record.id,
            audit_tool_detail_id=detail.id,
            claim_token=token,
            execution_node="node-1",
            new_tool_call_id="new-call-unknown",
        )
        assert execution is not None

        result = await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
        assert result.unknown_execution_records == 1
        assert result.unknown_execution_attempts == 1
        stored = await audit_crud.get_record(session, record.id)
        assert stored.status == AuditRecordStatus.EXECUTION_UNKNOWN
        stored_execution = await audit_crud.get_execution_record(session, execution.id)
        assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
async def test_startup_unknown_recovery_fails_pending_background_handoff(audit_database, tmp_path):
    audit_root = tmp_path / "audit"
    audit_file = audit_root / "temp_u1" / "audit_pending_handoff.json"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text("{}", encoding="utf-8")

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PASSED,
            tool_details=_tool_details(snapshot, conclusion="passed"),
            context_file_path=str(audit_file.resolve()),
        )
        _, token = await audit_crud.claim_passed_for_execution(session, audit_record_id=record.id)
        detail = (await audit_crud.list_tool_details(session, record.id))[0]
        execution = await audit_crud.create_execution_attempt(
            session,
            audit_record_id=record.id,
            audit_tool_detail_id=detail.id,
            claim_token=token,
            execution_node="node-1",
            new_tool_call_id="pending-handoff-call",
        )
        task = BackgroundTask(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            tool_call_id="pending-handoff-call",
            tool_name="execute_shell",
            status=BackgroundTaskStatus.PENDING,
            arguments={"command": "echo pending"},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            audit_record_id=record.id,
            audit_execution_record_id=execution.id,
        )
        session.add(task)
        await session.commit()

        await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
        await session.refresh(task)
        stored_task = await background_task_crud.get(session, task.id)
        active_tasks = await background_task_crud.list_active_user_tasks(session, uid="u1", session_id="session-1")
        pending_replies = await background_task_crud.list_pending_replies(session)
        stored_record = await audit_crud.get_record(session, record.id)
        stored_execution = await audit_crud.get_execution_record(session, execution.id)

    assert stored_task.status == BackgroundTaskStatus.FAILED
    assert stored_task.reply_status == BackgroundTaskReplyStatus.PENDING
    assert active_tasks == []
    assert [item.id for item in pending_replies] == [task.id]
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
async def test_startup_interrupted_confirmation_syncs_unknown_card(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(_uid, _session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)
    audit_root = tmp_path / "audit"
    audit_file = audit_root / "temp_u1" / "audit_confirmed_unknown.json"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text("{}", encoding="utf-8")

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PENDING,
            tool_details=_tool_details(snapshot),
            context_file_path=str(audit_file.resolve()),
            expires_at=get_local_time() + timedelta(minutes=10),
        )
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"audit_record_id": record.id, "status": AuditRecordStatus.PENDING.value}),
            is_processed=True,
        )
        session.add(card)
        await session.commit()
        claimed, token = await audit_crud.claim_pending_for_execution(
            session,
            audit_record_id=record.id,
            uid="u1",
            session_id="session-1",
            decision_message_id=20,
            decision_raw_message="同意",
            decided_by="tester",
        )
        detail = (await audit_crud.list_tool_details(session, record.id))[0]
        execution = await audit_crud.create_execution_attempt(
            session,
            audit_record_id=record.id,
            audit_tool_detail_id=detail.id,
            claim_token=token,
            execution_node="node-1",
            new_tool_call_id="confirmed-call-unknown",
        )
        assert claimed is not None and execution is not None

        await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
        await session.refresh(card)

        assert json.loads(card.content)["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
        assert [event["status"] for event in events] == [AuditRecordStatus.EXECUTION_UNKNOWN.value]


@pytest.mark.asyncio
async def test_execution_failure_marks_running_attempts_and_round_unknown(audit_database, tmp_path):
    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        context_file = (tmp_path / "audit_execution_unknown.json").resolve()
        context_file.write_text("{}", encoding="utf-8")
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.PASSED,
            tool_details=_tool_details(snapshot, conclusion="passed"),
            context_file_path=str(context_file),
        )
        _, token = await audit_crud.claim_passed_for_execution(session, audit_record_id=record.id)
        detail = (await audit_crud.list_tool_details(session, record.id))[0]
        execution = await audit_crud.create_execution_attempt(
            session,
            audit_record_id=record.id,
            audit_tool_detail_id=detail.id,
            claim_token=token,
            execution_node="node-1",
            new_tool_call_id="new-call-failed",
        )

        assert execution is not None
        assert await audit_crud.mark_execution_unknown(
            session,
            audit_record_id=record.id,
            claim_token=token,
            error_reason="工具执行异常，结果未知，禁止自动重试",
        )
        stored = await audit_crud.get_record(session, record.id)
        stored_execution = await audit_crud.get_execution_record(session, execution.id)
        assert stored.status == AuditRecordStatus.EXECUTION_UNKNOWN
        assert stored.execution_claim_token is None
        assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
async def test_startup_keeps_database_record_when_file_deletion_fails(audit_database, tmp_path, monkeypatch):
    from app.core.audit import startup as startup_module

    audit_root = tmp_path / "audit"
    audit_file = audit_root / "temp_u1" / "audit_1.json"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text("{}", encoding="utf-8")

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.BLOCKED,
            tool_details=_tool_details(snapshot, conclusion="blocked"),
            context_file_path=str(audit_file.resolve()),
        )

        def fail_cleanup(**kwargs):
            return AuditCleanupResult(failed_paths={str(audit_file): "locked"})

        monkeypatch.setattr(startup_module, "cleanup_audit_storage", fail_cleanup)
        result = await recover_and_cleanup_audit_data(session, retention_days=1, audit_root=audit_root)

        assert result.deleted_database_records == 0
        assert await audit_crud.get_record(session, record.id) is not None
        assert audit_file.exists()


@pytest.mark.asyncio
async def test_startup_deletes_file_before_expired_database_record(audit_database, tmp_path):
    audit_root = tmp_path / "audit"
    audit_file = audit_root / "temp_u1" / "audit_1.json"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text("{}", encoding="utf-8")
    old_timestamp = 1_000_000_000
    os.utime(audit_file, (old_timestamp, old_timestamp))

    async with audit_database() as session:
        record, snapshot = await _create_preparing(session, tmp_path, tool_count=1)
        await audit_crud.complete_preparation(
            session,
            audit_record_id=record.id,
            status=AuditRecordStatus.BLOCKED,
            tool_details=_tool_details(snapshot, conclusion="blocked"),
            context_file_path=str(audit_file.resolve()),
        )
        result = await recover_and_cleanup_audit_data(session, retention_days=1, audit_root=audit_root)
        assert not audit_file.exists()
        assert result.deleted_database_records == 1
        assert await audit_crud.get_record(session, record.id) is None
