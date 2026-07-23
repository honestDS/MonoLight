import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from starlette.websockets import WebSocketDisconnect

from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.api.v1 import chat as chat_api
from app.core.audit.confirmation import (
    get_pending_tool_results,
    persist_pending_confirmation_bundle,
    replace_pending_tool_result,
    supersede_persisted_pending_confirmation_bundle,
)
from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
    ERR_AUDIT_ROUND_BLOCKED,
    MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE,
    MSG_AUDIT_CONFIRMATION_SUPERSEDED,
    MSG_AUDIT_WAITING_CONFIRMATION,
)
from app.core.crud.audit import audit_crud
from app.core.i18n import t
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.message_platform import MessagePlatformStatus
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Message.__table__,
        AuditRecord.__table__,
        AuditToolDetail.__table__,
        AuditConfirmationClaim.__table__,
        AuditExecutionRecord.__table__,
        AuditToolResultVersion.__table__,
        ChatSession.__table__,
        SessionReplySequence.__table__,
        SessionReplyWorkItem.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def entry_dependencies(monkeypatch):
    profile = Profile(id=1, uid="owner", name="test", configs={})

    async def get_active(*_args, **_kwargs):
        return profile

    async def validate(*_args, **_kwargs):
        return None

    async def ensure_writable(*_args, **_kwargs):
        return None

    async def upsert_profile(*_args, **_kwargs):
        return None

    async def send_event(*_args, **_kwargs):
        return None

    async def generate_title(*_args, **_kwargs):
        return None

    for module_name in ("app.adapters.chat_web", "app.adapters.chat_ws", "app.adapters.weixin_openclaw.adapter"):
        monkeypatch.setattr(f"{module_name}.profile_crud.get_active", get_active)
        monkeypatch.setattr(f"{module_name}.ChatDispatcher.validate_initial_message_before_save", validate)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr("app.core.session_reply_queue.manager.session_crud.upsert_profile", upsert_profile)
    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_active_profile", generate_title)
    return profile


async def add_pending_confirmation(db: AsyncSession, *, uid: str = "owner", session_id: str = "session-1") -> AuditRecord:
    source_message = Message(
        session_id=session_id,
        uid=uid,
        profile_id=1,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content=InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo pending"})],
        ).model_dump_json(exclude_none=True),
        is_processed=True,
    )
    db.add(source_message)
    await db.flush()
    record = AuditRecord(
        uid=uid,
        operator_username="operator",
        session_id=session_id,
        source="http",
        language="zh",
        status=AuditRecordStatus.PENDING,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="round-hash",
        tool_count=1,
        intent_summary="执行需要确认的操作",
        expires_at=get_local_time() + timedelta(hours=1),
    )
    db.add(record)
    await db.flush()
    db.add(AuditConfirmationClaim(uid=uid, session_id=session_id, audit_record_id=record.id))
    db.add(
        Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-1",
                content=json.dumps(
                    {
                        "status": AuditRecordStatus.PENDING.value,
                        "error": "等待用户确认",
                        "reason": "测试操作需要确认",
                    },
                    ensure_ascii=False,
                ),
            ).model_dump_json(exclude_none=True),
            is_processed=True,
        )
    )
    db.add(
        Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=json.dumps({"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"}),
            is_processed=True,
        )
    )
    await db.commit()
    await db.refresh(record)
    return record


async def list_work(db: AsyncSession, session_id: str = "session-1") -> list[SessionReplyWorkItem]:
    result = await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.sequence_no))
    return list(result.scalars().all())


async def refresh_record(db: AsyncSession, record: AuditRecord) -> AuditRecord:
    await db.refresh(record)
    return record


async def get_confirmation_payload(db: AsyncSession, session_id: str = "session-1") -> dict:
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.type == MessageType.AUDIT_CONFIRMATION,
        )
        .order_by(Message.id.desc())
    )
    message = result.scalars().first()
    assert message is not None
    return json.loads(message.content)


async def get_tool_result_payload(db: AsyncSession, session_id: str = "session-1") -> dict:
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.type == MessageType.TOOL_RESULT,
        )
        .order_by(Message.id.desc())
    )
    message = result.scalars().first()
    assert message is not None
    stored_message = InternalMessage.model_validate_json(message.content)
    return json.loads(stored_message.content)


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
                content=json.dumps({"status": "pending"}),
            )
        ],
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"},
        dedupe_key=None,
    )

    current = await audit_crud.get_current_confirmation(db_session, uid="owner", session_id="session-1")
    assert current is not None
    assert [item.tool_call_id for item in stored_results] == ["call-atomic"]
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

    await replace_pending_tool_result(
        db_session,
        pending_message=pending["call-atomic"],
        original_tool_call_id="call-atomic",
        content=json.dumps({"status": "succeeded"}),
        audit_record_id=record.id,
    )
    await db_session.commit()
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.version_no))).scalars().all())
    assert [version.version_no for version in versions] == [0, 1]
    assert json.loads(InternalMessage.model_validate_json(versions[0].content).content)["status"] == "pending"
    assert json.loads(InternalMessage.model_validate_json(versions[1].content).content)["status"] == "succeeded"


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
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-superseded",
                content=json.dumps({"status": "pending"}),
            )
        ],
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"},
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
            tool_results=[InternalMessage(role=MessageRole.TOOL, tool_call_id="call-failure", content=json.dumps({"status": "pending"}))],
            confirmation_payload={"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"},
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
@pytest.mark.parametrize(
    ("message", "attachments", "expected_status", "expected_work_type"),
    [
        ("同意", None, AuditRecordStatus.EXECUTING, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION),
        ("拒绝", None, AuditRecordStatus.REJECTED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("继续做别的事", None, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("同意", ["attachment.txt"], AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("同意执行", None, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
    ],
)
async def test_web_entry_reaches_unified_submission_for_confirmation_outcomes(
    db_session: AsyncSession,
    entry_dependencies,
    monkeypatch,
    message,
    attachments,
    expected_status,
    expected_work_type,
):
    record = await add_pending_confirmation(db_session)

    async def wait_for_result(work_id):
        return {"work_id": work_id, "choices": []}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_result", wait_for_result)
    response = await web_chat_adapter.chat(
        db_session,
        message,
        uid="owner",
        session_id="session-1",
        attachments=attachments,
    )

    assert response["choices"] == []
    await refresh_record(db_session, record)
    works = await list_work(db_session)
    assert record.status == expected_status
    assert (await get_confirmation_payload(db_session))["status"] == expected_status.value
    assert len(works) == 1
    assert works[0].work_type == expected_work_type
    if expected_work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
        assert works[0].source_id == str(record.id)
    else:
        assert works[0].source_type.value == "user_message"
    if expected_status in {AuditRecordStatus.EXECUTING, AuditRecordStatus.REJECTED}:
        decision_result = await db_session.execute(select(Message).where(Message.session_id == "session-1", Message.type == MessageType.AUDIT_DECISION).order_by(Message.id.desc()))
        decision_message = decision_result.scalars().first()
        assert decision_message is not None
        assert decision_message.content == message
        assert parse_db_messages_to_internal([decision_message]) == []
        tool_result_payload = await get_tool_result_payload(db_session)
        assert tool_result_payload["confirmation_decision"] == message
        if expected_status == AuditRecordStatus.REJECTED:
            assert tool_result_payload["status"] == AuditRecordStatus.REJECTED.value
            assert tool_result_payload["confirmation_status"] == "reject"
            assert tool_result_payload["rejection_source"] == "user"
            assert tool_result_payload["reason"] == "测试操作需要确认"
            assert tool_result_payload["error"] == t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER, locale="zh")
            assert tool_result_payload["error"] != t(MSG_AUDIT_WAITING_CONFIRMATION, locale="zh")
            assert tool_result_payload["error"] != t(ERR_AUDIT_ROUND_BLOCKED, locale="zh")
    if expected_status == AuditRecordStatus.CANCELLED:
        text_result = await db_session.execute(select(Message).where(Message.session_id == "session-1", Message.type == MessageType.TEXT).order_by(Message.id.desc()))
        text_message = text_result.scalars().first()
        assert text_message is not None
        assert text_message.content == message
        tool_result_payload = await get_tool_result_payload(db_session)
        assert tool_result_payload["status"] == AuditRecordStatus.CANCELLED.value
        assert tool_result_payload["confirmation_status"] == "invalid_input"
        assert "用户未正确输入安全审计确认关键词" in tool_result_payload["error"]
        assert "安全审计已阻止本轮工具调用" not in tool_result_payload["error"]


@pytest.mark.asyncio
async def test_web_entry_without_pending_confirmation_treats_decision_as_normal_message(db_session, entry_dependencies, monkeypatch):
    async def wait_for_result(work_id):
        return {"work_id": work_id, "choices": []}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_result", wait_for_result)
    await web_chat_adapter.chat(db_session, "同意", uid="owner", session_id="session-1")

    works = await list_work(db_session)
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
    result = await db_session.execute(select(Message).where(Message.session_id == "session-1").order_by(Message.id.desc()))
    assert result.scalars().first().type == MessageType.TEXT


@pytest.mark.asyncio
async def test_web_entry_expires_confirmation_and_tool_result_before_queuing_message(db_session, entry_dependencies, monkeypatch):
    record = await add_pending_confirmation(db_session)
    record.expires_at = get_local_time() - timedelta(seconds=1)
    await db_session.commit()

    async def wait_for_result(work_id):
        return {"work_id": work_id, "choices": []}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_result", wait_for_result)
    response = await web_chat_adapter.chat(db_session, "执行另一个操作", uid="owner", session_id="session-1")

    assert response["choices"] == []
    await refresh_record(db_session, record)
    tool_result_payload = await get_tool_result_payload(db_session)
    works = await list_work(db_session)
    assert record.status == AuditRecordStatus.EXPIRED
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.EXPIRED.value
    assert tool_result_payload["status"] == AuditRecordStatus.EXPIRED.value
    assert tool_result_payload["confirmation_status"] == AuditRecordStatus.EXPIRED.value
    assert "安全审计确认已过期" in tool_result_payload["error"]
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY


@pytest.mark.asyncio
async def test_websocket_adapter_approval_reaches_confirmed_execution_work(db_session, entry_dependencies, monkeypatch):
    record = await add_pending_confirmation(db_session)

    async def wait_for_stream(work_id):
        yield {"type": "done", "session_id": "session-1", "response": {"work_id": work_id}}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", wait_for_stream)
    events = [
        event
        async for event in ws_chat_adapter.chat(
            db_session,
            "approve",
            uid="owner",
            session_id="session-1",
            request_id="request-1",
        )
    ]

    await refresh_record(db_session, record)
    works = await list_work(db_session)
    assert events == [
        {
            "type": "input_queued",
            "session_id": "session-1",
            "request_id": "request-1",
            "work_id": works[0].id,
            "submission_status": "approved",
        },
        {
            "type": "done",
            "session_id": "session-1",
            "response": {"work_id": works[0].id},
            "request_id": "request-1",
        },
    ]
    assert record.status == AuditRecordStatus.EXECUTING
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.EXECUTING.value
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert works[0].execution_state["request_ids"] == ["request-1"]


@pytest.mark.asyncio
async def test_websocket_route_active_task_appends_approval_through_unified_submission(db_session, entry_dependencies, monkeypatch):
    record = await add_pending_confirmation(db_session)
    first_started = asyncio.Event()
    session_id = "session-1"

    async def held_chat(*_args, **_kwargs):
        first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return
        yield {"type": "done", "session_id": session_id}

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    class FakeWebSocket:
        query_params = {}

        def __init__(self):
            self.sent: list[dict] = []
            self.receive_count = 0

        async def accept(self):
            return None

        async def receive_json(self):
            if self.receive_count == 0:
                self.receive_count += 1
                return {"session_id": session_id, "message": "等待中的首条消息"}
            if self.receive_count == 1:
                self.receive_count += 1
                await first_started.wait()
                return {"session_id": session_id, "message": "同意", "request_id": "approval-request"}
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, data):
            self.sent.append(data)

    monkeypatch.setattr(chat_api, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(chat_api.system_setting_crud, "get_runtime_settings", lambda *_args, **_kwargs: _runtime_settings())
    monkeypatch.setattr(chat_api, "ensure_web_session_writable", _noop_async)
    monkeypatch.setattr(chat_api.ChatDispatcher, "validate_initial_message_before_save", _noop_async)
    monkeypatch.setattr(chat_api.session_notifier, "register", _noop_async)
    monkeypatch.setattr(chat_api.session_notifier, "unregister", _noop_async)
    monkeypatch.setattr(chat_api.profile_crud, "get_active", _active_profile)
    monkeypatch.setattr(chat_api.ws_chat_adapter, "chat", held_chat)

    websocket = FakeWebSocket()
    await chat_api.chat_websocket(websocket, SimpleNamespace(uid="owner"))

    await refresh_record(db_session, record)
    works = await list_work(db_session)
    assert record.status == AuditRecordStatus.EXECUTING
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.EXECUTING.value
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert websocket.sent == [
        {
            "type": "input_queued",
            "session_id": session_id,
            "request_id": "approval-request",
            "work_id": works[0].id,
            "submission_status": "approved",
        }
    ]


async def _noop_async(*_args, **_kwargs):
    return None


async def _runtime_settings():
    return SimpleNamespace(log_locale="zh")


async def _active_profile(*_args, **_kwargs):
    return Profile(id=1, uid="owner", name="test", configs={})


def build_weixin_adapter() -> WeixinOpenClawAdapter:
    adapter = object.__new__(WeixinOpenClawAdapter)
    adapter.config = WeixinOpenClawConfig()
    adapter.context_tokens = {}
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "attachments", "raw", "expected_status", "expected_work_type"),
    [
        ("同意", [], {}, AuditRecordStatus.EXECUTING, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION),
        ("同意", ["/tmp/file.txt"], {}, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("同意", [], {"quote": {"content": "原消息"}}, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("同意执行", [], {}, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("approve continue", [], {}, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
    ],
)
async def test_weixin_entry_preserves_strict_confirmation_boundary(
    db_session: AsyncSession,
    entry_dependencies,
    message,
    attachments,
    raw,
    expected_status,
    expected_work_type,
):
    record = await add_pending_confirmation(db_session)
    adapter = build_weixin_adapter()
    inbound = WeixinOpenClawMessage(
        user_id="weixin-user",
        text=message,
        session_id="session-1",
        attachments=attachments,
        raw=raw,
    )

    assert await adapter.handle_message(db_session, inbound, uid="owner") is True

    await refresh_record(db_session, record)
    works = await list_work(db_session)
    assert record.status == expected_status
    assert (await get_confirmation_payload(db_session))["status"] == expected_status.value
    assert len(works) == 1
    assert works[0].work_type == expected_work_type


@pytest.mark.asyncio
async def test_weixin_entry_without_pending_confirmation_queues_decision_as_normal_message(db_session, entry_dependencies):
    adapter = build_weixin_adapter()
    inbound = WeixinOpenClawMessage(user_id="weixin-user", text="同意", session_id="session-1")

    assert await adapter.handle_message(db_session, inbound, uid="owner") is True

    works = await list_work(db_session)
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY


@pytest.mark.asyncio
async def test_repeated_weixin_approval_cannot_create_second_confirmed_execution(db_session, entry_dependencies):
    record = await add_pending_confirmation(db_session)
    adapter = build_weixin_adapter()
    inbound = WeixinOpenClawMessage(user_id="weixin-user", text="继续", session_id="session-1")

    assert await adapter.handle_message(db_session, inbound, uid="owner") is True
    assert await adapter.handle_message(db_session, inbound, uid="owner") is True

    works = await list_work(db_session)
    assert sum(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in works) == 1
    assert len(works) == 2
    await refresh_record(db_session, record)
    assert record.status == AuditRecordStatus.EXECUTING


@pytest.mark.asyncio
async def test_weixin_platform_flushes_old_batch_before_independent_decision(monkeypatch):
    handler = WeixinOpenClawPlatformHandler()
    platform = SimpleNamespace(
        id=1,
        uid="owner",
        is_enabled=True,
        platform_type=handler.platform_type,
        status=MessagePlatformStatus.CONNECTED,
        account_id="account",
        config={},
        state={},
        get_config_secret=lambda key: "token" if key == "token" else "",
    )
    disabled_platform = SimpleNamespace(**{**platform.__dict__, "status": MessagePlatformStatus.DISCONNECTED})
    old_message = WeixinOpenClawMessage(user_id="weixin-user", text="旧批次", session_id="session-1")
    decision_message = WeixinOpenClawMessage(user_id="weixin-user", text="同意", session_id="session-1")
    order: list[str] = []

    class FakeAdapter:
        config = SimpleNamespace(poll_interval_ms=0)
        sync_buf = ""

        def __init__(self):
            self.poll_count = 0

        async def poll_messages_once(self):
            self.poll_count += 1
            return [old_message, decision_message] if self.poll_count == 1 else []

        async def close(self):
            return None

    adapter = FakeAdapter()
    get_count = 0

    async def get_platform(*_args, **_kwargs):
        nonlocal get_count
        get_count += 1
        return platform if get_count <= 2 else disabled_platform

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    async def handle_message(*args, **_kwargs):
        order.append(args[1].text)

    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.message_platform_crud.get", get_platform)
    monkeypatch.setattr("app.core.message_platforms.weixin_openclaw.message_platform_crud.update_runtime_state", _noop_async)
    monkeypatch.setattr(handler, "_build_adapter", lambda _platform: adapter)
    monkeypatch.setattr(handler, "_handle_message", handle_message)

    await handler.run(1)

    assert order == ["旧批次", "同意"]
