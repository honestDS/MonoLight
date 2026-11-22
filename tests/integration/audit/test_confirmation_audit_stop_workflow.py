import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.service as audit_service_module
import app.core.background_tasks.external_work as external_work_module
import app.core.confirmation.events as confirmation_events_module
import app.core.confirmation.service as confirmation_service_module
import app.providers.database as database_provider
from app.core.constants import (
    ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_audit as executor_audit_module
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.tools import TOOL_EXECUTOR_MAP
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.audit.confirmation_workflow_support import _confirmation_workflow_session_factory as _confirmation_workflow_session_factory
from tests.integration.audit.confirmation_workflow_support import (
    _profile_config,
    _seed_pending_confirmation,
    _stop_confirmation_session,
)


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
            return await confirmation_service_module.prepare_tool_round(
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
                execution_scope="foreground",
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
        confirmed_tool_calls=[
            InternalToolCall(
                id="original-shell-call-1",
                name="file_tool",
                arguments={"operation": "read", "path": str(tmp_path / "started.txt")},
            ),
            InternalToolCall(
                id="original-shell-call-2",
                name="file_tool",
                arguments={"operation": "read", "path": str(tmp_path / "remaining.txt")},
            ),
        ],
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

    paths: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class ControlledFileExecutor:
        requires_audit = True

        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **arguments) -> str:
            paths.append(arguments["path"])
            assert len(paths) == 1
            first_started.set()
            await release_first.wait()
            if outcome == "interrupted":
                raise AssertionError("interrupted file call was not cancelled")
            if outcome == "success":
                return json.dumps({"status": "success", "operation": "read", "content": "controlled success"})
            return json.dumps({"status": "failed", "operation": "read", "error": "controlled failure"})

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, "file_tool", ControlledFileExecutor)

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

    assert paths == [str(tmp_path / "started.txt")]
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
    result_messages = {message.audit_tool_call_id: InternalMessage.model_validate_json(message.content or "{}") for message in structured_results}
    result_payloads = {call_id: json.loads(result_message.content or "{}") for call_id, result_message in result_messages.items()}
    first_result = result_messages["original-shell-call-1"]
    first_payload = result_payloads["original-shell-call-1"]
    assert first_payload["confirmation_decision"] == "approve"
    assert get_tool_execution_status(first_result) == expected_first_execution_status.value
    if outcome == "success":
        assert first_payload["status"] == "success"
        assert first_payload["operation"] == "read"
        assert first_payload["content"] == "controlled success"
    elif outcome == "failed":
        assert first_payload["status"] == "failed"
        assert first_payload["operation"] == "read"
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
@pytest.mark.parametrize(
    ("stop_point", "task_started"),
    [
        pytest.param("waiting", False, id="waiting-pending-task"),
        pytest.param("waiting", True, id="waiting-started-task"),
        pytest.param("before-wait", False, id="running-before-wait"),
        pytest.param("before-receipt", False, id="before-receipt-pending-task"),
        pytest.param("before-receipt", True, id="before-receipt-started-task"),
    ],
)
async def test_confirmed_background_task_stop_preserves_audit_receipt_and_adds_cancel_marker(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stop_point: str,
    task_started: bool,
) -> None:
    original_tool_call = InternalToolCall(
        id="original-confirmed-background-call",
        name="execute_shell",
        arguments={"command": "echo approved background", "execution_mode": "non_interactive"},
    )
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        confirmed_tool_calls=[original_tool_call],
    )

    for module in (
        database_provider,
        consumer_module,
        executor_lifecycle_module,
        executor_audit_module,
        external_work_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", confirmation_workflow_session_factory)

    async def capture_confirmation_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_confirmation_event)

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
    assert submitted_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION

    worker_id = "confirmed-background-stop-worker"
    async with confirmation_workflow_session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed is not None
    assert claimed.id == submitted_work.id
    consumer = consumer_module.SessionReplyConsumer()
    wait_gate_entered = asyncio.Event()
    release_wait_gate = asyncio.Event()
    consumer_task: asyncio.Task | None = None
    if stop_point == "before-wait":
        original_build_terminal_wait_response = executor_confirmed_module.build_terminal_wait_response

        async def gated_build_terminal_wait_response(*args, **kwargs):
            wait_gate_entered.set()
            await release_wait_gate.wait()
            return await original_build_terminal_wait_response(*args, **kwargs)

        monkeypatch.setattr(executor_confirmed_module, "build_terminal_wait_response", gated_build_terminal_wait_response)
    elif stop_point == "before-receipt":
        original_append_confirmed_tool_result = executor_confirmed_module._append_confirmed_tool_result

        async def gated_append_confirmed_tool_result(*args, **kwargs):
            wait_gate_entered.set()
            await release_wait_gate.wait()
            return await original_append_confirmed_tool_result(*args, **kwargs)

        monkeypatch.setattr(executor_confirmed_module, "_append_confirmed_tool_result", gated_append_confirmed_tool_result)

    try:
        if stop_point in {"before-wait", "before-receipt"}:
            consumer_task = asyncio.create_task(consumer._run_claimed(claimed.id, worker_id, claimed.attempt_count, claimed.max_attempts))
            await asyncio.wait_for(wait_gate_entered.wait(), timeout=5)
        else:
            await consumer._run_claimed(claimed.id, worker_id, claimed.attempt_count, claimed.max_attempts)

        async with confirmation_workflow_session_factory() as db:
            waiting_work = await db.get(SessionReplyWorkItem, submitted_work.id)
            record = await db.get(AuditRecord, audit_record_id)
            queued_results = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.audit_record_id == audit_record_id,
                            Message.audit_tool_call_id == original_tool_call.id,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            tasks = list((await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == "session-confirmation"))).scalars().all())
            versions = list(
                (
                    await db.execute(
                        select(AuditToolResultVersion)
                        .where(
                            AuditToolResultVersion.audit_record_id == audit_record_id,
                            AuditToolResultVersion.original_tool_call_id == original_tool_call.id,
                        )
                        .order_by(AuditToolResultVersion.version_no)
                    )
                )
                .scalars()
                .all()
            )
            assert waiting_work is not None
            assert waiting_work.status == (SessionReplyWorkStatus.WAITING_EXTERNAL_WORK if stop_point == "waiting" else SessionReplyWorkStatus.RUNNING)
            assert record is not None and record.status == AuditRecordStatus.EXECUTING
            assert len(queued_results) == 1
            queued_result = queued_results[0]
            assert queued_result.id is not None
            queued_result_id = queued_result.id
            original_tool_result_content = queued_result.content
            queued_internal = InternalMessage.model_validate_json(queued_result.content or "{}")
            assert queued_internal.tool_call_id == original_tool_call.id
            queued_payload = json.loads(queued_internal.content or "{}")
            assert len(tasks) == 1
            task = tasks[0]
            assert task.id is not None
            task_id = task.id
            if task_started:
                claimed_task = await background_task_crud.try_claim(
                    db,
                    task_id=task_id,
                    worker_id="confirmed-background-task-worker",
                )
                assert claimed_task is not None
                task = claimed_task
            if stop_point == "before-receipt":
                assert queued_payload["status"] == AuditRecordStatus.EXECUTING.value
                execution_state = waiting_work.execution_state or {}
                external_task_ids = [slot.get("task_id") for slot in (execution_state.get("pending_tools") if isinstance(execution_state.get("pending_tools"), list) else []) if isinstance(slot, dict)]
                assert task_id not in external_task_ids
                assert queued_result_id not in (execution_state.get("external_tool_result_message_ids") or [])
            else:
                assert queued_payload["status"] == "queued"
                assert queued_payload["task_id"] == task_id
            assert task.tool_call_id != original_tool_call.id
            assert task.audit_record_id == audit_record_id
            assert task.audit_execution_record_id is not None
            execution = await db.get(AuditExecutionRecord, task.audit_execution_record_id)
            assert execution is not None
            assert execution.audit_record_id == audit_record_id
            assert execution.status == AuditExecutionStatus.RUNNING
            assert execution.new_tool_call_id == task.tool_call_id
            version_snapshots_before = [(version.original_tool_call_id, version.version_no, version.content) for version in versions]

        stop_payload = await _stop_confirmation_session(confirmation_workflow_session_factory)
        assert stop_payload == {"session_id": "session-confirmation", "cancelled_count": 1}
        if stop_point in {"before-wait", "before-receipt"}:
            release_wait_gate.set()
            await asyncio.gather(consumer_task, return_exceptions=True)
        repeated_stop = await _stop_confirmation_session(confirmation_workflow_session_factory)
        assert repeated_stop == {"session_id": "session-confirmation", "cancelled_count": 0}
    finally:
        release_wait_gate.set()
        if consumer_task is not None and not consumer_task.done():
            consumer_task.cancel()
        if consumer_task is not None:
            await asyncio.gather(consumer_task, return_exceptions=True)

    async with confirmation_workflow_session_factory() as db:
        cancelled_work = await db.get(SessionReplyWorkItem, submitted_work.id)
        cancelled_record = await db.get(AuditRecord, audit_record_id)
        cancelled_task = await db.get(BackgroundTask, task_id)
        persisted_queued_result = await db.get(Message, queued_result_id)
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())
        final_versions = list(
            (
                await db.execute(
                    select(AuditToolResultVersion)
                    .where(
                        AuditToolResultVersion.audit_record_id == audit_record_id,
                        AuditToolResultVersion.original_tool_call_id == original_tool_call.id,
                    )
                    .order_by(AuditToolResultVersion.version_no)
                )
            )
            .scalars()
            .all()
        )
        markers = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation", Message.type == MessageType.BACKGROUND_TASK_RESULT).order_by(Message.id))).scalars().all())
        session_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == "session-confirmation").order_by(SessionEvent.id))).scalars().all())
        work_status = await session_reply_queue_manager.get_work_status(db, submitted_work.id, uid="owner")
        history = await message_crud.get_history_paged(
            db,
            session_id="session-confirmation",
            uid="owner",
            limit=100,
            include_tool_messages=True,
        )

    expected_audit_status = AuditRecordStatus.EXECUTION_UNKNOWN if task_started else AuditRecordStatus.CANCELLED
    expected_execution_status = AuditExecutionStatus.EXECUTION_UNKNOWN if task_started else AuditExecutionStatus.CANCELLED
    expected_marker_status = "execution_unknown" if task_started else "failed"
    assert cancelled_work is not None
    assert cancelled_work.status == SessionReplyWorkStatus.CANCELLED
    assert cancelled_work.locked_by is None and cancelled_work.lock_until is None
    assert cancelled_record is not None and cancelled_record.status == expected_audit_status
    assert cancelled_task is not None
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.tool_call_id != original_tool_call.id
    assert cancelled_task.audit_record_id == audit_record_id
    assert cancelled_task.audit_execution_record_id is not None
    assert (cancelled_task.started_at is not None) is task_started
    assert len(executions) == 1
    assert executions[0].status == expected_execution_status
    assert persisted_queued_result is not None
    assert persisted_queued_result.content == original_tool_result_content
    assert [(version.original_tool_call_id, version.version_no, version.content) for version in final_versions] == version_snapshots_before
    assert len(markers) == 1
    marker = markers[0]
    assert marker.work_id is None
    marker_payload = json.loads(marker.content or "{}")
    assert marker_payload["type"] == "background_tool_result"
    assert marker_payload["task_id"] == task_id
    assert marker_payload["original_tool_result_message_id"] == queued_result_id
    assert marker_payload["tool_cancelled"] is True
    assert marker_payload["execution_status"] == expected_marker_status
    assert marker_payload["tool_call"] == {
        "id": original_tool_call.id,
        "name": original_tool_call.name,
        "arguments": original_tool_call.arguments,
    }
    assert marker_payload["task"]["status"] == "cancelled"
    assert marker_payload["task"]["error"] == (PROMPT_TOOL_INTERRUPTED if task_started else t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION))
    assert marker.id in (cancelled_work.execution_state or {}).get("external_tool_result_message_ids", [])
    assert len(session_events) == 1
    event = session_events[0].event
    assert event["type"] == "tool_results_update"
    event_message = next(message for message in event["messages"] if message["id"] == marker.id)
    assert event_message["id"] == marker.id
    assert event_message["type"] == MessageType.BACKGROUND_TASK_RESULT.value
    assert event_message["content"] == marker.content
    assert work_status["status"] == SessionReplyWorkStatus.CANCELLED.value
    status_event = next(event for event in work_status["session_events"] if event["type"] == "tool_results_update")
    status_message = next(message for message in status_event["messages"] if message["id"] == marker.id)
    assert status_message["content"] == marker.content
    history_marker = next(message for message in history if message.id == marker.id)
    assert history_marker.type == MessageType.BACKGROUND_TASK_RESULT
    assert history_marker.work_id is None
    assert history_marker.content == marker.content


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
            await confirmation_service_module.prepare_tool_round(
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
                execution_scope="foreground",
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
