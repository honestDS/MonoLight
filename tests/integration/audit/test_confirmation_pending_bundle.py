import json
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.session_reply_queue.manager_submission as session_reply_queue_manager_module
from app.core.audit import confirmation
from app.core.audit.confirmation import (
    get_pending_tool_results,
    persist_pending_confirmation_bundle,
    replace_pending_tool_result,
    supersede_persisted_pending_confirmation_bundle,
)
from app.core.constants import (
    MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE,
    MSG_AUDIT_CONFIRMATION_SUPERSEDED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.i18n import t
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditRecord,
    AuditRecordStatus,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from tests.integration.audit.test_confirmation_entry_flows import (
    add_pending_confirmation,
    get_confirmation_payload,
    get_tool_result_payload,
    list_work,
)
from tests.integration.audit.test_confirmation_entry_flows import (
    db_session as _db_session,
)
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result

db_session = _db_session


@pytest.mark.asyncio
async def test_confirmed_tool_result_replacement_keeps_one_original_message_chain(db_session: AsyncSession):
    source_message = Message(
        session_id="session-1",
        uid="owner",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(id="call-1", name="safe_tool", arguments={"value": 1}),
                InternalToolCall(id="call-2", name="safe_tool", arguments={"value": 2}),
            ],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add(source_message)
    await db_session.flush()
    pending_messages = []
    for call_id in ("call-1", "call-2"):
        pending_message = Message(
            session_id="session-1",
            uid="owner",
            profile_id=1,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=call_id,
                content=json.dumps({"status": "pending", "confirmation_decision": "同意"}),
            ).model_dump_json(exclude_none=True),
            is_processed=True,
        )
        pending_messages.append(pending_message)
        db_session.add(pending_message)
    db_session.add(
        Message(
            session_id="session-1",
            uid="owner",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content="{}",
            is_processed=True,
        )
    )
    decision_message = Message(
        session_id="session-1",
        uid="owner",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.AUDIT_DECISION,
        content="同意",
        is_processed=True,
    )
    db_session.add(decision_message)
    await db_session.commit()

    assert decision_message.type != MessageType.TEXT
    assert parse_db_messages_to_internal([decision_message]) == []

    pending_by_call_id = await get_pending_tool_results(
        db_session,
        uid="owner",
        session_id="session-1",
        source_assistant_message_id=source_message.id,
        before_message_id=decision_message.id,
        tool_call_ids=["call-1", "call-2"],
    )
    assert pending_by_call_id is not None
    assert set(pending_by_call_id) == {"call-1", "call-2"}

    await replace_pending_tool_result(
        db_session,
        pending_message=pending_by_call_id["call-1"],
        original_tool_call_id="call-1",
        content=json.dumps({"status": "success"}),
    )
    await replace_pending_tool_result(
        db_session,
        pending_message=pending_by_call_id["call-2"],
        original_tool_call_id="call-2",
        content=json.dumps({"status": "success"}),
    )
    await db_session.commit()

    result = await db_session.execute(select(Message).where(Message.session_id == "session-1").where(Message.uid == "owner").where(Message.type.in_([MessageType.TOOL_CALL, MessageType.TOOL_RESULT])).order_by(Message.id.asc()))
    stored_messages = list(result.scalars().all())
    assert [message.type for message in stored_messages] == [
        MessageType.TOOL_CALL,
        MessageType.TOOL_RESULT,
        MessageType.TOOL_RESULT,
    ]
    assert [message.id for message in stored_messages[1:]] == [message.id for message in pending_messages]
    assert [InternalMessage.model_validate_json(message.content).tool_call_id for message in stored_messages[1:]] == ["call-1", "call-2"]
    assert all(json.loads(InternalMessage.model_validate_json(message.content).content)["status"] == "success" for message in stored_messages[1:])
    assert all(json.loads(InternalMessage.model_validate_json(message.content).content)["confirmation_decision"] == "同意" for message in stored_messages[1:])


@pytest.mark.asyncio
async def test_pending_tool_result_validation_rejects_missing_and_duplicate_results(db_session: AsyncSession):
    record = await add_pending_confirmation(db_session)
    decision_message = Message(
        session_id="session-1",
        uid="owner",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.AUDIT_DECISION,
        content="同意",
        is_processed=True,
    )
    db_session.add(decision_message)
    await db_session.commit()

    assert (
        await get_pending_tool_results(
            db_session,
            uid="owner",
            session_id="session-1",
            source_assistant_message_id=record.source_assistant_message_id,
            before_message_id=decision_message.id,
            tool_call_ids=["call-1", "call-2"],
        )
        is None
    )

    result = await db_session.execute(select(Message).where(Message.session_id == "session-1", Message.type == MessageType.TOOL_RESULT))
    pending_tool_result = result.scalars().one()

    async def set_tool_result_payload(payload: dict):
        pending_tool_result.content = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-1",
            content=json.dumps(payload),
        ).model_dump_json(exclude_none=True)
        db_session.add(pending_tool_result)
        await db_session.commit()

    async def get_single_tool_result():
        return await get_pending_tool_results(
            db_session,
            uid="owner",
            session_id="session-1",
            source_assistant_message_id=record.source_assistant_message_id,
            before_message_id=decision_message.id,
            tool_call_ids=["call-1"],
        )

    await set_tool_result_payload(
        {
            "status": AuditRecordStatus.EXECUTING.value,
            "confirmation_status": "approve",
            "confirmation_decision": "同意",
        }
    )
    assert await get_single_tool_result() is not None

    await set_tool_result_payload(
        {
            "status": AuditRecordStatus.EXECUTING.value,
            "confirmation_status": "ignore",
            "confirmation_decision": "忽略",
        }
    )
    assert await get_single_tool_result() is not None

    await set_tool_result_payload(
        {
            "status": AuditRecordStatus.EXECUTING.value,
            "confirmation_status": "approve",
        }
    )
    assert await get_single_tool_result() is None

    await set_tool_result_payload(
        {
            "status": AuditRecordStatus.EXECUTING.value,
            "confirmation_decision": "同意",
        }
    )
    assert await get_single_tool_result() is None

    db_session.add(
        Message(
            session_id="session-1",
            uid="owner",
            profile_id=1,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps({"status": "pending"}),
            ).model_dump_json(exclude_none=True),
            is_processed=True,
        )
    )
    await db_session.commit()

    second_decision_message = Message(
        session_id="session-1",
        uid="owner",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="同意",
        is_processed=True,
    )
    db_session.add(second_decision_message)
    await db_session.commit()

    assert (
        await get_pending_tool_results(
            db_session,
            uid="owner",
            session_id="session-1",
            source_assistant_message_id=record.source_assistant_message_id,
            before_message_id=second_decision_message.id,
            tool_call_ids=["call-1"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_pending_confirmation_becomes_visible_with_structured_results_atomically(db_session: AsyncSession):
    source_message = Message(
        session_id="session-1",
        uid="owner",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-atomic", name="safe_tool", arguments={"value": 1})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add(source_message)
    await db_session.flush()
    record = AuditRecord(
        uid="owner",
        operator_username="operator",
        session_id="session-1",
        source="http",
        language="zh",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="atomic-round",
        tool_count=1,
        expires_at=get_local_time() + timedelta(hours=1),
    )
    db_session.add_all([record, ChatSession(session_id="session-1", uid="owner", profile_id=1)])
    await db_session.commit()

    assert await audit_crud.get_current_confirmation(db_session, uid="owner", session_id="session-1") is None
    pending_content = {
        "type": "files_to_user",
        "status": AuditRecordStatus.PENDING.value,
        "files": [
            {
                "id": "download-token",
                "name": "report.pdf",
                "download_url": "/api/v1/download-sent?token=download-token",
            }
        ],
        "errors": [{"path": "/allowed/reports/missing.pdf", "error": "file not found"}],
        "allowed_operation_dirs": ["/allowed/reports"],
    }

    stored_results, _card = await persist_pending_confirmation_bundle(
        db_session,
        audit_record_id=record.id,
        uid="owner",
        session_id="session-1",
        profile_id=1,
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-atomic",
                content=json.dumps(pending_content),
            )
        ],
        confirmation_payload=audit_confirmation_payload(
            record.id,
            expires_at=record.expires_at.isoformat(),
            timeout_seconds=3600,
            locale=record.language,
        ),
        dedupe_key=None,
    )

    current = await audit_crud.get_current_confirmation(db_session, uid="owner", session_id="session-1")
    assert current is not None
    assert [item.tool_call_id for item in stored_results] == ["call-atomic"]
    assert json.loads(stored_results[0].content) == pending_content
    pending = await get_pending_tool_results(
        db_session,
        uid="owner",
        session_id="session-1",
        source_assistant_message_id=source_message.id,
        before_message_id=source_message.id,
        tool_call_ids=["call-atomic"],
        audit_record_id=record.id,
    )
    assert pending is not None
    assert set(pending) == {"call-atomic"}
    pending_message = pending["call-atomic"]
    pending_message.model_context_suffix = "<current_session_todo_snapshot>{}</current_session_todo_snapshot>"
    db_session.add(pending_message)
    await db_session.flush()

    replacement_content = {
        "type": "files_to_user",
        "status": "succeeded",
        "files": [
            {
                "id": "download-token",
                "name": "report.pdf",
                "download_url": "/api/v1/download-sent?token=download-token",
            }
        ],
        "errors": [{"path": "/allowed/reports/missing.pdf", "error": "file not found"}],
        "allowed_operation_dirs": ["/allowed/reports"],
    }
    replacement_content_json = json.dumps(replacement_content)
    returned_content = await replace_pending_tool_result(
        db_session,
        pending_message=pending_message,
        original_tool_call_id="call-atomic",
        content=replacement_content_json,
        audit_record_id=record.id,
    )
    assert returned_content == replacement_content_json
    await db_session.commit()
    await db_session.refresh(pending_message)
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.version_no))).scalars().all())
    assert [version.version_no for version in versions] == [0, 1]
    assert json.loads(InternalMessage.model_validate_json(versions[0].content).content) == pending_content
    assert json.loads(InternalMessage.model_validate_json(versions[1].content).content) == replacement_content
    assert pending_message.model_context_suffix is None


@pytest.mark.asyncio
async def test_superseding_persisted_pending_bundle_cancels_structured_results(db_session: AsyncSession):
    source_message = Message(
        session_id="session-superseded",
        uid="owner",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-superseded", name="safe_tool", arguments={})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add(source_message)
    await db_session.flush()
    record = AuditRecord(
        uid="owner",
        operator_username="operator",
        session_id="session-superseded",
        source="http",
        language="zh",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="superseded-round",
        tool_count=1,
        expires_at=get_local_time() + timedelta(hours=1),
    )
    db_session.add_all([record, ChatSession(session_id="session-superseded", uid="owner", profile_id=1)])
    await db_session.commit()

    await persist_pending_confirmation_bundle(
        db_session,
        audit_record_id=record.id,
        uid="owner",
        session_id="session-superseded",
        profile_id=1,
        tool_results=[pending_audit_tool_result("call-superseded")],
        confirmation_payload=audit_confirmation_payload(
            record.id,
            expires_at=record.expires_at.isoformat(),
            timeout_seconds=3600,
            locale=record.language,
        ),
        dedupe_key=None,
    )

    cancelled_results = await supersede_persisted_pending_confirmation_bundle(
        db_session,
        audit_record_id=record.id,
        uid="owner",
        session_id="session-superseded",
    )

    await db_session.refresh(record)
    current_confirmation = await audit_crud.get_current_confirmation(
        db_session,
        uid="owner",
        session_id="session-superseded",
    )
    claims = list((await db_session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))).scalars().all())
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.version_no))).scalars().all())

    assert record.status == AuditRecordStatus.CANCELLED
    assert current_confirmation is None
    assert claims == []
    assert len(cancelled_results) == 1
    cancelled_payload = json.loads(cancelled_results[0].content)
    assert cancelled_payload["status"] == AuditRecordStatus.CANCELLED.value
    assert cancelled_payload["confirmation_status"] == "superseded"
    assert cancelled_payload["error"] == t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale="zh")
    assert cancelled_payload["error"] != t(MSG_AUDIT_CONFIRMATION_SUPERSEDED, locale="zh")
    assert t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale="en") == "The pending operation was cancelled because a new user message was received. Re-evaluate it using the latest user message."
    assert [version.version_no for version in versions] == [0, 1]
    assert [json.loads(InternalMessage.model_validate_json(version.content).content)["status"] for version in versions] == [
        AuditRecordStatus.PENDING.value,
        AuditRecordStatus.CANCELLED.value,
    ]


@pytest.mark.asyncio
async def test_new_tool_call_cancellation_invalidates_summary_and_versions_pending_results(db_session: AsyncSession):
    source_message = Message(
        session_id="session-new-tool-call",
        uid="owner",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-new-tool", name="safe_tool", arguments={})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add(source_message)
    await db_session.flush()
    record = AuditRecord(
        uid="owner",
        operator_username="operator",
        session_id="session-new-tool-call",
        source="http",
        language="en",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="new-tool-call-round",
        tool_count=1,
        expires_at=get_local_time() + timedelta(hours=1),
    )
    session = ChatSession(session_id="session-new-tool-call", uid="owner", profile_id=1)
    db_session.add_all([record, session])
    await db_session.commit()

    stored_results, _card = await persist_pending_confirmation_bundle(
        db_session,
        audit_record_id=record.id,
        uid="owner",
        session_id="session-new-tool-call",
        profile_id=1,
        tool_results=[pending_audit_tool_result("call-new-tool")],
        confirmation_payload=audit_confirmation_payload(
            record.id,
            expires_at=record.expires_at.isoformat(),
            timeout_seconds=3600,
            locale=record.language,
        ),
        dedupe_key=None,
    )
    assert stored_results[0].id is not None

    session.context_summary = "包含待确认操作的旧总结"
    session.context_summary_message_id = stored_results[0].id
    session.context_summary_revision = 5
    session.context_content_revision = 11
    db_session.add(session)
    await db_session.commit()

    cancelled = await confirmation.cancel_confirmation_by_session(
        db_session,
        uid="owner",
        session_id="session-new-tool-call",
        locale="en",
    )

    await db_session.refresh(record)
    await db_session.refresh(session)
    tool_result_payload = await get_tool_result_payload(db_session, "session-new-tool-call")
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.version_no))).scalars().all())

    assert cancelled == 1
    assert record.status == AuditRecordStatus.CANCELLED
    assert tool_result_payload["status"] == AuditRecordStatus.CANCELLED.value
    assert tool_result_payload["confirmation_status"] == "superseded"
    assert tool_result_payload["error"] == t(MSG_AUDIT_CONFIRMATION_SUPERSEDED, locale="en")
    assert [json.loads(InternalMessage.model_validate_json(version.content).content)["status"] for version in versions] == [
        AuditRecordStatus.PENDING.value,
        AuditRecordStatus.CANCELLED.value,
    ]
    assert session.context_summary is None
    assert session.context_summary_message_id is None
    assert session.context_content_revision == 12


@pytest.mark.asyncio
async def test_pending_confirmation_bundle_rolls_back_all_rows_when_activation_fails(db_session: AsyncSession, monkeypatch):
    source_message = Message(
        session_id="session-failure",
        uid="owner",
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-failure", name="safe_tool", arguments={})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db_session.add(source_message)
    await db_session.flush()
    record = AuditRecord(
        uid="owner",
        operator_username="operator",
        session_id="session-failure",
        source="http",
        language="zh",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="failure-round",
        tool_count=1,
        expires_at=get_local_time() + timedelta(hours=1),
    )
    db_session.add_all([record, ChatSession(session_id="session-failure", uid="owner", profile_id=1)])
    await db_session.commit()

    async def fail_activation(*_args, **_kwargs):
        raise RuntimeError("injected activation failure")

    monkeypatch.setattr(audit_crud, "activate_confirmation_claim", fail_activation)
    with pytest.raises(RuntimeError, match="injected activation failure"):
        await persist_pending_confirmation_bundle(
            db_session,
            audit_record_id=record.id,
            uid="owner",
            session_id="session-failure",
            profile_id=1,
            tool_results=[pending_audit_tool_result("call-failure")],
            confirmation_payload=audit_confirmation_payload(
                record.id,
                expires_at=record.expires_at.isoformat(),
                timeout_seconds=3600,
                locale=record.language,
            ),
            dedupe_key=None,
        )

    await db_session.refresh(record)
    stored_messages = list((await db_session.execute(select(Message).where(Message.session_id == "session-failure").order_by(Message.id))).scalars().all())
    stored_versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id))).scalars().all())
    stored_claims = list((await db_session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))).scalars().all())
    assert record.status == AuditRecordStatus.AUDIT_FAILED
    assert [message.type for message in stored_messages] == [MessageType.TOOL_CALL]
    assert stored_versions == []
    assert stored_claims == []


@pytest.mark.asyncio
async def test_invalid_confirmation_input_rolls_back_bundle_when_work_enqueue_fails(db_session: AsyncSession, monkeypatch):
    record = await add_pending_confirmation(db_session)
    events: list[dict] = []

    async def no_expiration(*_args, **_kwargs):
        return 0

    async def send_event(_uid, _session_id, event):
        events.append(event)

    async def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected work enqueue failure")

    monkeypatch.setattr(session_reply_queue_manager_module, "expire_confirmation_by_session", no_expiration)
    monkeypatch.setattr(session_reply_queue_manager, "_enqueue_foreground_message", fail_enqueue)
    monkeypatch.setattr("app.core.audit.confirmation_events.send_session_event", send_event)

    with pytest.raises(RuntimeError, match="injected work enqueue failure"):
        await session_reply_queue_manager.submit_user_message(
            db_session,
            uid="owner",
            session_id="session-1",
            profile=Profile(id=1, uid="owner", name="test", configs={}),
            message="继续做别的事",
            attachments=None,
            source="http",
        )

    await db_session.refresh(record)
    claims = list((await db_session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record.id))).scalars().all())
    text_messages = list((await db_session.execute(select(Message).where(Message.session_id == "session-1", Message.type == MessageType.TEXT))).scalars().all())

    assert record.status == AuditRecordStatus.PENDING
    assert claims
    assert text_messages == []
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.PENDING.value
    assert (await get_tool_result_payload(db_session))["status"] == AuditRecordStatus.PENDING.value
    assert await list_work(db_session) == []
    assert events == []


@pytest.mark.asyncio
async def test_invalid_confirmation_input_commits_bundle_before_broadcast(db_session: AsyncSession, monkeypatch):
    record = await add_pending_confirmation(db_session)
    events: list[dict] = []
    commit_count = 0
    original_commit = db_session.commit
    original_broadcast = session_reply_queue_manager_module.broadcast_pending_confirmation_cancellation

    async def no_expiration(*_args, **_kwargs):
        return 0

    async def counted_commit():
        nonlocal commit_count
        commit_count += 1
        await original_commit()

    async def send_event(_uid, _session_id, event):
        events.append(event)

    async def broadcast_after_commit(db, *, cancellation):
        assert commit_count == 1
        await original_broadcast(db, cancellation=cancellation)

    monkeypatch.setattr(session_reply_queue_manager_module, "expire_confirmation_by_session", no_expiration)
    monkeypatch.setattr(db_session, "commit", counted_commit)
    monkeypatch.setattr(session_reply_queue_manager_module, "broadcast_pending_confirmation_cancellation", broadcast_after_commit)
    monkeypatch.setattr("app.core.audit.confirmation_events.send_session_event", send_event)

    initial_message, work, status, direct_events = await session_reply_queue_manager.submit_user_message(
        db_session,
        uid="owner",
        session_id="session-1",
        profile=Profile(id=1, uid="owner", name="test", configs={}),
        message="继续做别的事",
        attachments=None,
        source="http",
    )

    await db_session.refresh(record)
    works = await list_work(db_session)

    assert commit_count == 1
    assert status == "cancelled"
    assert initial_message.id is not None
    assert record.status == AuditRecordStatus.CANCELLED
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.CANCELLED.value
    assert (await get_tool_result_payload(db_session))["status"] == AuditRecordStatus.CANCELLED.value
    assert [item.id for item in works] == [work.id]
    assert [event["type"] for event in direct_events] == ["audit_confirmation_status", "audit_tool_results_update"]
    assert direct_events[0]["status"] == AuditRecordStatus.CANCELLED.value
    direct_tool_result = InternalMessage.model_validate_json(direct_events[1]["messages"][0]["content"])
    assert json.loads(direct_tool_result.content)["status"] == AuditRecordStatus.CANCELLED.value
    assert [event["type"] for event in events] == ["audit_confirmation_status", "audit_tool_results_update"]
