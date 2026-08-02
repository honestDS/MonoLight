import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from starlette.websockets import WebSocketDisconnect

import app.core.session_reply_queue.manager as session_reply_queue_manager_module
from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.api.v1 import chat as chat_api
from app.core.audit import confirmation
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
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
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
    AuditToolConclusion,
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
    SessionReplyWorkStatus,
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


@pytest_asyncio.fixture
async def concurrent_confirmation_session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'confirmation-concurrency.db'}",
        connect_args={"timeout": 30},
    )
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
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture
def entry_dependencies(monkeypatch):
    profile = Profile(id=1, uid="owner", name="test", configs={})

    async def resolve_profile(*_args, **_kwargs):
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
        monkeypatch.setattr(f"{module_name}.resolve_profile_for_session", resolve_profile)
        monkeypatch.setattr(f"{module_name}.ChatDispatcher.validate_initial_message_before_save", validate)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)
    monkeypatch.setattr("app.core.session_reply_queue.manager.session_crud.upsert_profile", upsert_profile)
    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)
    monkeypatch.setattr("app.adapters.weixin_openclaw.adapter.generate_session_title_for_selected_profile", generate_title)
    return profile


async def add_pending_confirmation(
    db: AsyncSession,
    *,
    uid: str = "owner",
    session_id: str = "session-1",
    high_risk: bool = False,
) -> AuditRecord:
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
    if high_risk:
        db.add(
            AuditToolDetail(
                audit_record_id=record.id,
                original_tool_call_id="call-1",
                turn_index=0,
                tool_name="execute_shell",
                conclusion=AuditToolConclusion.PENDING,
                score=8,
                reason="测试高危操作需要确认",
                arguments_hash="a" * 64,
                arguments_summary="{}",
            )
        )
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
            content=json.dumps(
                {
                    "type": "audit_confirmation",
                    "audit_record_id": record.id,
                    "status": "pending",
                    "confirmation_mode": "standard",
                }
            ),
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


async def _prepare_concurrent_confirmation(session_factory, *, expires_at=None) -> int:
    async with session_factory() as db:
        record = await add_pending_confirmation(db)
        if expires_at is not None:
            record.expires_at = expires_at
        db.add(ChatSession(session_id="session-1", uid="owner", profile_id=1))
        await db.commit()
        assert record.id is not None
        return record.id


async def _collect_websocket_submission(session_factory, *, message: str, request_id: str, role: str, completed: asyncio.Event | None = None):
    try:
        async with session_factory() as db:
            db.info["confirmation_race_role"] = role
            return [
                event
                async for event in ws_chat_adapter.chat(
                    db,
                    message,
                    uid="owner",
                    session_id="session-1",
                    request_id=request_id,
                )
            ]
    finally:
        if completed is not None:
            completed.set()


async def _collect_web_submission(session_factory, *, message: str, role: str, completed: asyncio.Event | None = None):
    try:
        async with session_factory() as db:
            db.info["confirmation_race_role"] = role
            return await web_chat_adapter.chat(
                db,
                message,
                uid="owner",
                session_id="session-1",
            )
    finally:
        if completed is not None:
            completed.set()


def _install_confirmation_read_barrier(monkeypatch, *, delayed_role: str | None = None, delayed_until: asyncio.Event | None = None):
    original_get_current = audit_crud.get_current_confirmation
    both_read = asyncio.Event()
    release = asyncio.Event()
    readers: set[str] = set()

    async def get_current_after_both_read(db, *, uid: str, session_id: str):
        record = await original_get_current(db, uid=uid, session_id=session_id)
        role = str(db.info.get("confirmation_race_role") or id(db))
        readers.add(role)
        if len(readers) == 2:
            both_read.set()
        await release.wait()
        if role == delayed_role and delayed_until is not None:
            await delayed_until.wait()
        return record

    monkeypatch.setattr(audit_crud, "get_current_confirmation", get_current_after_both_read)
    return original_get_current, both_read, release


async def _concurrent_confirmation_snapshot(session_factory, record_id: int):
    async with session_factory() as db:
        record = await db.get(AuditRecord, record_id)
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == "session-1").order_by(SessionReplyWorkItem.sequence_no))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-1", Message.role == MessageRole.USER).order_by(Message.id))).scalars().all())
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == record_id))).scalars().all())
    return record, works, messages, claims


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
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"},
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
        pending_message=pending["call-atomic"],
        original_tool_call_id="call-atomic",
        content=replacement_content_json,
        audit_record_id=record.id,
    )
    assert returned_content == replacement_content_json
    await db_session.commit()
    versions = list((await db_session.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.version_no))).scalars().all())
    assert [version.version_no for version in versions] == [0, 1]
    assert json.loads(InternalMessage.model_validate_json(versions[0].content).content) == pending_content
    assert json.loads(InternalMessage.model_validate_json(versions[1].content).content) == replacement_content


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
        tool_results=[
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id="call-new-tool",
                content=json.dumps({"status": AuditRecordStatus.PENDING.value}),
            )
        ],
        confirmation_payload={"type": "audit_confirmation", "audit_record_id": record.id, "status": "pending"},
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
        ("忽略", None, AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
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
    direct_events = response["session_events"]
    assert [event["type"] for event in direct_events] == ["audit_confirmation_status", "audit_tool_results_update"]
    assert direct_events[0]["session_id"] == "session-1"
    assert direct_events[0]["audit_record_id"] == record.id
    assert direct_events[0]["status"] == expected_status.value
    assert direct_events[1]["session_id"] == "session-1"
    assert direct_events[1]["audit_record_id"] == record.id
    direct_tool_result = InternalMessage.model_validate_json(direct_events[1]["messages"][0]["content"])
    assert json.loads(direct_tool_result.content)["status"] == expected_status.value
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
@pytest.mark.parametrize(
    ("message", "expected_status", "expected_work_type"),
    [
        ("同意", AuditRecordStatus.CANCELLED, SessionReplyWorkType.FOREGROUND_REPLY),
        ("忽略", AuditRecordStatus.EXECUTING, SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION),
    ],
)
async def test_web_entry_uses_audit_detail_for_high_risk_confirmation_mode(
    db_session: AsyncSession,
    entry_dependencies,
    monkeypatch,
    message,
    expected_status,
    expected_work_type,
):
    record = await add_pending_confirmation(db_session, high_risk=True)
    assert (await get_confirmation_payload(db_session))["confirmation_mode"] == "standard"

    async def wait_for_result(work_id):
        return {"work_id": work_id, "choices": []}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_result", wait_for_result)
    response = await web_chat_adapter.chat(
        db_session,
        message,
        uid="owner",
        session_id="session-1",
    )

    assert response["choices"] == []
    await refresh_record(db_session, record)
    works = await list_work(db_session)
    tool_result_payload = await get_tool_result_payload(db_session)
    assert record.status == expected_status
    assert (await get_confirmation_payload(db_session))["status"] == expected_status.value
    assert len(works) == 1
    assert works[0].work_type == expected_work_type
    if message == "同意":
        assert tool_result_payload["status"] == AuditRecordStatus.CANCELLED.value
        assert tool_result_payload["confirmation_status"] == "invalid_input"
        assert "忽略" in tool_result_payload["error"]
    else:
        assert works[0].source_id == str(record.id)
        assert tool_result_payload["confirmation_status"] == "ignore"
        assert tool_result_payload["confirmation_decision"] == "忽略"


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
    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

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
    monkeypatch.setattr("app.core.audit.confirmation.send_session_event", send_event)

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
    assert [event["type"] for event in events] == ["audit_confirmation_status", "audit_tool_results_update", "done"]
    assert events[0]["session_id"] == "session-1"
    assert events[0]["audit_record_id"] == record.id
    assert events[0]["status"] == AuditRecordStatus.EXECUTING.value
    assert events[0]["request_id"] == "request-1"
    assert events[1]["session_id"] == "session-1"
    assert events[1]["audit_record_id"] == record.id
    assert events[1]["request_id"] == "request-1"
    direct_tool_result = InternalMessage.model_validate_json(events[1]["messages"][0]["content"])
    assert json.loads(direct_tool_result.content)["status"] == AuditRecordStatus.EXECUTING.value
    assert events[2] == {
        "type": "done",
        "session_id": "session-1",
        "response": {"work_id": works[0].id},
        "request_id": "request-1",
    }
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
    monkeypatch.setattr(chat_api, "resolve_profile_for_session", _active_profile)
    monkeypatch.setattr(chat_api.ws_chat_adapter, "chat", held_chat)

    websocket = FakeWebSocket()
    await chat_api.chat_websocket(websocket, SimpleNamespace(uid="owner"))

    await refresh_record(db_session, record)
    works = await list_work(db_session)
    assert record.status == AuditRecordStatus.EXECUTING
    assert (await get_confirmation_payload(db_session))["status"] == AuditRecordStatus.EXECUTING.value
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert [event["type"] for event in websocket.sent] == ["audit_confirmation_status", "audit_tool_results_update"]
    assert websocket.sent[0]["session_id"] == session_id
    assert websocket.sent[0]["audit_record_id"] == record.id
    assert websocket.sent[0]["status"] == AuditRecordStatus.EXECUTING.value
    assert websocket.sent[0]["request_id"] == "approval-request"
    assert websocket.sent[1]["session_id"] == session_id
    assert websocket.sent[1]["audit_record_id"] == record.id
    assert websocket.sent[1]["request_id"] == "approval-request"
    direct_tool_result = InternalMessage.model_validate_json(websocket.sent[1]["messages"][0]["content"])
    assert json.loads(direct_tool_result.content)["status"] == AuditRecordStatus.EXECUTING.value


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
@pytest.mark.parametrize("decision_text", ["同意", "继续", "拒绝", "忽略"])
async def test_weixin_platform_flushes_old_batch_before_independent_decision(monkeypatch, decision_text):
    handler = WeixinOpenClawPlatformHandler()
    platform = SimpleNamespace(
        id=1,
        uid="owner",
        is_enabled=True,
        use_stream_dispatch=False,
        platform_type=handler.platform_type,
        status=MessagePlatformStatus.CONNECTED,
        account_id="account",
        config={},
        state={},
        get_config_secret=lambda key: "token" if key == "token" else "",
    )
    disabled_platform = SimpleNamespace(**{**platform.__dict__, "status": MessagePlatformStatus.DISCONNECTED})
    old_message = WeixinOpenClawMessage(user_id="weixin-user", text="旧批次", session_id="session-1")
    decision_message = WeixinOpenClawMessage(user_id="weixin-user", text=decision_text, session_id="session-1")
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

    assert order == ["旧批次", decision_text]


@pytest.mark.asyncio
async def test_concurrent_websocket_approvals_keep_losing_input_as_absorbable_foreground_work(
    concurrent_confirmation_session_factory,
    entry_dependencies,
    monkeypatch,
):
    record_id = await _prepare_concurrent_confirmation(concurrent_confirmation_session_factory)

    async def completed_stream(work_id):
        yield {"type": "done", "session_id": "session-1", "work_id": work_id, "response": {"work_id": work_id}}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", completed_stream)
    _original_current, both_read, release = _install_confirmation_read_barrier(monkeypatch)

    first_task = asyncio.create_task(
        _collect_websocket_submission(
            concurrent_confirmation_session_factory,
            message="同意",
            request_id="approval-a",
            role="first-websocket",
        )
    )
    second_task = asyncio.create_task(
        _collect_websocket_submission(
            concurrent_confirmation_session_factory,
            message="approve",
            request_id="approval-b",
            role="second-websocket",
        )
    )
    await both_read.wait()
    release.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)

    assert all(event.get("type") != "error" for event in [*first_events, *second_events])
    record, works, messages, claims = await _concurrent_confirmation_snapshot(concurrent_confirmation_session_factory, record_id)
    assert record is not None
    assert record.status == AuditRecordStatus.EXECUTING
    assert claims == []
    assert sum(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in works) == 1
    assert len(works) == 2
    assert sum(message.content == "同意" for message in messages) == 1
    assert sum(message.content == "approve" for message in messages) == 1

    confirmed_work = next(work for work in works if work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION)
    appended_work = next(work for work in works if work.work_type == SessionReplyWorkType.FOREGROUND_REPLY)
    assert appended_work.sequence_no > confirmed_work.sequence_no
    assert appended_work.execution_state["message_source"] == "ws"
    assert appended_work.execution_state["request_ids"] in [["approval-a"], ["approval-b"]]

    async with concurrent_confirmation_session_factory() as db:
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="confirmation-worker", lease_seconds=300)
        assert claimed is not None
        assert claimed.id == confirmed_work.id
        additional = await session_reply_queue_manager.absorb_contiguous_foreground_messages(
            db,
            work_id=claimed.id,
            worker_id="confirmation-worker",
        )
        assert additional is not None
        assert additional.messages[0].content in {"同意", "approve"}
        absorbed_work = await db.get(SessionReplyWorkItem, appended_work.id)
        assert absorbed_work is not None
        assert absorbed_work.status == SessionReplyWorkStatus.MERGED
        absorbed_message = await db.get(Message, int(appended_work.source_id))
        assert absorbed_message is not None
        assert absorbed_message.is_processed is True


@pytest.mark.asyncio
async def test_concurrent_weixin_approval_and_websocket_rejection_preserve_both_inputs_once(
    concurrent_confirmation_session_factory,
    entry_dependencies,
    monkeypatch,
):
    record_id = await _prepare_concurrent_confirmation(concurrent_confirmation_session_factory)
    rejection_completed = asyncio.Event()

    async def completed_stream(work_id):
        yield {"type": "done", "session_id": "session-1", "work_id": work_id, "response": {"work_id": work_id}}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", completed_stream)
    _original_current, both_read, release = _install_confirmation_read_barrier(
        monkeypatch,
        delayed_role="weixin-approval",
        delayed_until=rejection_completed,
    )
    adapter = build_weixin_adapter()

    async def submit_weixin_approval():
        async with concurrent_confirmation_session_factory() as db:
            db.info["confirmation_race_role"] = "weixin-approval"
            return await adapter.handle_message(
                db,
                WeixinOpenClawMessage(user_id="weixin-user", text="继续", session_id="session-1"),
                uid="owner",
            )

    approval_task = asyncio.create_task(submit_weixin_approval())
    rejection_task = asyncio.create_task(
        _collect_websocket_submission(
            concurrent_confirmation_session_factory,
            message="拒绝",
            request_id="rejection-request",
            role="websocket-rejection",
            completed=rejection_completed,
        )
    )
    await both_read.wait()
    release.set()
    approval_result, rejection_events = await asyncio.gather(approval_task, rejection_task)

    assert approval_result is True
    assert all(event.get("type") != "error" for event in rejection_events)
    record, works, messages, claims = await _concurrent_confirmation_snapshot(concurrent_confirmation_session_factory, record_id)
    assert record is not None
    assert record.status == AuditRecordStatus.REJECTED
    assert claims == []
    assert sum(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in works) == 0
    assert len(works) == 2
    assert works[1].sequence_no > works[0].sequence_no
    assert sum(message.content == "继续" for message in messages) == 1
    assert sum(message.content == "拒绝" for message in messages) == 1
    assert sum(message.type == MessageType.AUDIT_DECISION for message in messages) == 1
    assert sum(message.type == MessageType.TEXT for message in messages) == 1


@pytest.mark.asyncio
async def test_concurrent_approval_and_foreground_append_leave_the_second_work_ready(
    concurrent_confirmation_session_factory,
    entry_dependencies,
    monkeypatch,
):
    record_id = await _prepare_concurrent_confirmation(concurrent_confirmation_session_factory)
    append_completed = asyncio.Event()

    async def completed_stream(work_id):
        yield {"type": "done", "session_id": "session-1", "work_id": work_id, "response": {"work_id": work_id}}

    async def completed_result(work_id):
        return {"work_id": work_id, "choices": []}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", completed_stream)
    monkeypatch.setattr(session_reply_queue_manager, "wait_for_result", completed_result)
    _original_current, both_read, release = _install_confirmation_read_barrier(
        monkeypatch,
        delayed_role="websocket-approval",
        delayed_until=append_completed,
    )

    approval_task = asyncio.create_task(
        _collect_websocket_submission(
            concurrent_confirmation_session_factory,
            message="同意",
            request_id="approval-request",
            role="websocket-approval",
        )
    )
    append_task = asyncio.create_task(
        _collect_web_submission(
            concurrent_confirmation_session_factory,
            message="继续做别的事",
            role="web-append",
            completed=append_completed,
        )
    )
    await both_read.wait()
    release.set()
    approval_events, append_response = await asyncio.gather(approval_task, append_task)

    assert all(event.get("type") != "error" for event in approval_events)
    assert append_response["choices"] == []
    record, works, messages, claims = await _concurrent_confirmation_snapshot(concurrent_confirmation_session_factory, record_id)
    assert record is not None
    assert record.status == AuditRecordStatus.CANCELLED
    assert claims == []
    assert sum(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in works) == 0
    assert len(works) == 2
    assert works[1].sequence_no > works[0].sequence_no
    assert works[1].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert sum(message.content == "同意" for message in messages) == 1
    assert sum(message.content == "继续做别的事" for message in messages) == 1
    assert all(message.type == MessageType.TEXT for message in messages)


@pytest.mark.asyncio
async def test_concurrent_approval_and_expiration_cleanup_fall_back_without_a_dangling_claim(
    concurrent_confirmation_session_factory,
    entry_dependencies,
    monkeypatch,
):
    record_id = await _prepare_concurrent_confirmation(concurrent_confirmation_session_factory)
    original_get_current = audit_crud.get_current_confirmation
    approval_read = asyncio.Event()
    cleanup_read = asyncio.Event()
    release = asyncio.Event()
    cleanup_completed = asyncio.Event()

    async def held_approval_current(db, *, uid: str, session_id: str):
        record = await original_get_current(db, uid=uid, session_id=session_id)
        if db.info.get("confirmation_race_role") == "approval":
            approval_read.set()
            await cleanup_read.wait()
            await release.wait()
            await cleanup_completed.wait()
        return record

    async def completed_stream(work_id):
        yield {"type": "done", "session_id": "session-1", "work_id": work_id, "response": {"work_id": work_id}}

    monkeypatch.setattr(audit_crud, "get_current_confirmation", held_approval_current)
    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", completed_stream)

    async def expire_after_shared_read():
        try:
            await approval_read.wait()
            async with concurrent_confirmation_session_factory() as db:
                current = await original_get_current(db, uid="owner", session_id="session-1")
                assert current is not None
                cleanup_read.set()
                await release.wait()
                record = await db.get(AuditRecord, record_id)
                assert record is not None
                record.expires_at = get_local_time() - timedelta(seconds=1)
                db.add(record)
                await db.commit()
                await confirmation.expire_confirmation_by_session(db, uid="owner", session_id="session-1")
        finally:
            cleanup_completed.set()

    approval_task = asyncio.create_task(
        _collect_websocket_submission(
            concurrent_confirmation_session_factory,
            message="同意",
            request_id="expired-approval",
            role="approval",
        )
    )
    cleanup_task = asyncio.create_task(expire_after_shared_read())
    await approval_read.wait()
    await cleanup_read.wait()
    release.set()
    approval_events, _cleanup_result = await asyncio.gather(approval_task, cleanup_task)

    assert all(event.get("type") != "error" for event in approval_events)
    record, works, messages, claims = await _concurrent_confirmation_snapshot(concurrent_confirmation_session_factory, record_id)
    assert record is not None
    assert record.status == AuditRecordStatus.EXPIRED
    assert claims == []
    assert len(works) == 1
    assert works[0].work_type == SessionReplyWorkType.FOREGROUND_REPLY
    assert sum(message.content == "同意" for message in messages) == 1
    assert all(message.type == MessageType.TEXT for message in messages)
