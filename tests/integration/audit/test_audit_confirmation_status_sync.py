import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.core.audit.startup import recover_audit_state
from app.core.confirmation import ConfirmationDecision, notify_confirmation_tool_results, update_confirmation_message_status, update_confirmation_tool_results_for_decision
from app.core.constants import ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from tests.integration.audit.test_audit_records import _create_preparing, _tool_details
from tests.integration.audit.test_audit_records import audit_database as _audit_database
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result

audit_database = _audit_database


async def _add_pending_confirmation_messages(session, record, *, include_card=True):
    await session.refresh(record)
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
        content=pending_audit_tool_result("call-1").model_dump_json(exclude_none=True),
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
            content=json.dumps(audit_confirmation_payload(record.id, expires_at=record.expires_at.isoformat())),
            is_processed=True,
        )
        session.add(card)
    await session.commit()
    return tool_result, card


@pytest.mark.asyncio
async def test_confirmation_status_sync_uses_database_status_and_deduplicates_broadcast(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append((uid, session_id, event))

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)

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
        await session.refresh(record)
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps(audit_confirmation_payload(record.id, summary="run command", expires_at=record.expires_at.isoformat())),
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

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)

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
        await session.refresh(record)
        tool_result, _card = await _add_pending_confirmation_messages(session, record, include_card=False)
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps(audit_confirmation_payload(record.id, expires_at=record.expires_at.isoformat())),
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

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)

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
        card_payload = json.loads(card.content)
        card_payload["status"] = AuditRecordStatus.REJECTED.value
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

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)

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
            content=json.dumps(audit_confirmation_payload(record.id)),
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

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)

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
                content=json.dumps(audit_confirmation_payload(record.id)),
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
            content=json.dumps(audit_confirmation_payload(record.id)),
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
                conflict_payload = audit_confirmation_payload(record.id)
                conflict_payload["status"] = AuditRecordStatus.SUCCEEDED.value
                await db.execute(update(Message).where(Message.id == card.id).values(content=json.dumps(conflict_payload)))
                await db.commit()
                return False
            return await original_update(db, **kwargs)

        monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)
        monkeypatch.setattr("app.core.confirmation.events.message_crud.update_content_if_matches", conflicting_update)

        assert not await update_confirmation_message_status(session, audit_record_id=record.id)
        await session.refresh(card)

        assert json.loads(card.content)["status"] == AuditRecordStatus.SUCCEEDED.value
        assert events == []


@pytest.mark.asyncio
async def test_startup_expiration_syncs_confirmation_card_from_final_record(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(uid, session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)
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

        await recover_audit_state(session)
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

        await recover_audit_state(session)

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
async def test_startup_interrupted_confirmation_syncs_unknown_card(audit_database, tmp_path, monkeypatch):
    events = []

    async def send_event(_uid, _session_id, event):
        events.append(event)

    monkeypatch.setattr("app.core.confirmation.events.send_session_event", send_event)
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
        await session.refresh(record)
        card = Message(
            uid="u1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps(audit_confirmation_payload(record.id, expires_at=record.expires_at.isoformat())),
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

        await recover_audit_state(session)
        await session.refresh(card)

        assert json.loads(card.content)["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
        assert [event["status"] for event in events] == [AuditRecordStatus.EXECUTION_UNKNOWN.value]
