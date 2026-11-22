import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.adapters.chat_web import web_chat_adapter
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.api.v1 import chat as chat_api
from app.core.confirmation import events as confirmation_events_module
from app.core.security import get_current_user
from app.core.session.notifier import SessionNotifier
from app.core.session_reply_queue import manager_submission as manager_submission_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.audit import AuditRecord, AuditRecordStatus, AuditToolDetail
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User
from app.providers.database import get_db
from tests.integration.audit.confirmation_workflow_support import _confirmation_workflow_session_factory as _confirmation_workflow_session_factory
from tests.integration.audit.confirmation_workflow_support import _seed_pending_confirmation
from tests.integration.chat.test_chat_websocket_permissions import _Socket


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
