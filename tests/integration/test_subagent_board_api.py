import json
import time
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.chat import router
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.message import Message, MessageContentFormat, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from app.models.user import User
from app.providers.database import get_db


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as session:
            yield session


def _build_app(db: AsyncSession, auth_state: dict[str, object]) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield db

    def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(
            uid=auth_state["uid"],
            is_superuser=auth_state["is_superuser"],
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest_asyncio.fixture
async def api_context(db_session: AsyncSession):
    auth_state: dict[str, object] = {"uid": "board-api-owner", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield db_session, auth_state, client


def _profile_configs(marker: str) -> dict[str, Any]:
    configs = ProfileConfig.model_validate({}).model_dump(mode="json")
    configs["marker"] = marker
    return configs


async def _add_user(db: AsyncSession, uid: str, *, is_superuser: bool = False) -> User:
    user = User(uid=uid, username=f"{uid}-name", is_superuser=is_superuser)
    db.add(user)
    await db.flush()
    return user


async def _add_profile(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
    name: str,
    is_default: bool = False,
) -> Profile:
    profile = Profile(
        id=profile_id,
        uid=uid,
        name=name,
        configs=_profile_configs(name),
        is_default=is_default,
    )
    db.add(profile)
    await db.flush()
    return profile


async def _add_session(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile_id: int | None = None,
    profile_override_id: int | None = None,
) -> ChatSession:
    session = ChatSession(
        session_id=session_id,
        uid=uid,
        profile_id=profile_id,
        profile_override_id=profile_override_id,
        source="http",
    )
    db.add(session)
    await db.flush()
    return session


def _snapshot(
    work_id: int,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    completed: bool = True,
) -> dict[str, Any]:
    return {
        "work_id": work_id,
        "current_work_id": work_id,
        "completed": completed,
        "status": status,
        "result": result,
        "error": error,
    }


async def _add_agent_round(
    db: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    name: str,
    status: SessionReplyWorkStatus,
    subagent: Subagent | None = None,
    active: bool = False,
    round_result: dict[str, Any] | None = None,
    error: str | None = None,
    lock_until: int | None = None,
    execution_state: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    posts: list[tuple[str, str]] | None = None,
) -> tuple[Subagent, SubagentWork, SessionReplyWorkItem]:
    created_at = created_at or datetime(2026, 1, 1) + timedelta(seconds=work_id)
    updated_at = updated_at or created_at
    if subagent is None:
        subagent = Subagent(
            id=f"board-api-agent-{work_id}",
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            name=name,
            active_child_work_id=work_id if active else None,
            latest_child_work_id=work_id,
            last_called_at=created_at,
            created_at=created_at,
        )
    else:
        subagent.latest_child_work_id = work_id
        subagent.active_child_work_id = work_id if active else None
        subagent.last_called_at = created_at
    db.add(subagent)
    await db.flush()

    if status == SessionReplyWorkStatus.RUNNING and lock_until is None:
        lock_until = int(time.time()) + 300
    state = dict(execution_state or {})
    state["child_work_id"] = work_id
    work = SessionReplyWorkItem(
        id=work_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        subagent_id=subagent.id,
        sequence_no=work_id,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.PARENT_WORK,
        source_id="board-api-parent",
        dedupe_key=f"board-api-child-{work_id}",
        status=status,
        execution_state=state,
        locked_by=f"board-api-worker-{work_id}" if status == SessionReplyWorkStatus.RUNNING else None,
        lock_until=lock_until,
        error=error,
        created_at=created_at,
        updated_at=updated_at,
    )
    db.add(work)
    await db.flush()

    round_row = SubagentWork(
        child_work_id=work_id,
        subagent_id=subagent.id,
        current_work_id=work_id,
        result=round_result,
        created_at=created_at,
    )
    db.add(round_row)
    await db.flush()

    for index, (stage, content) in enumerate(posts or []):
        db.add(
            SubagentBoardPost(
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                subagent_id=subagent.id,
                child_work_id=work_id,
                stage=stage,
                content=content,
                created_at=created_at + timedelta(seconds=index),
            )
        )
    await db.flush()
    return subagent, round_row, work


async def _add_parent_work(
    db: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
) -> SessionReplyWorkItem:
    work = SessionReplyWorkItem(
        id=work_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        sequence_no=work_id,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="board-api-user-message",
        dedupe_key=f"board-api-parent-{work_id}",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={"message_source": "http"},
    )
    db.add(work)
    await db.flush()
    return work


def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 200
    return body["data"]


def _error(response: httpx.Response, status_code: int) -> None:
    assert response.status_code == status_code, response.text
    assert response.json()["code"] == status_code


@pytest.mark.asyncio
async def test_board_list_returns_active_recent_and_only_latest_round(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    active_specs = [
        (2, "queued", SessionReplyWorkStatus.READY_FOR_LLM, None, "queued"),
        (3, "running", SessionReplyWorkStatus.RUNNING, int(time.time()) + 300, "running"),
        (4, "waiting", SessionReplyWorkStatus.WAITING_EXTERNAL_WORK, None, "waiting"),
        (5, "expired", SessionReplyWorkStatus.RUNNING, 0, "expired"),
    ]
    for work_id, name, status, lock_until, stage in active_specs:
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=name,
            status=status,
            active=True,
            lock_until=lock_until,
            posts=[(stage, f"{name} preview")],
        )

    for work_id in (6, 7):
        completed = _snapshot(
            work_id,
            status="succeeded",
            result={"content": f"recent result {work_id}", "files": None, "skip_reply": False},
        )
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=f"recent-{work_id}",
            status=SessionReplyWorkStatus.SUCCEEDED,
            round_result=completed,
            updated_at=datetime(2026, 1, 1) + timedelta(minutes=work_id),
            posts=[("succeeded", "terminal post")],
        )

    old_snapshot = _snapshot(
        10,
        status="succeeded",
        result={"content": "old round result", "files": None, "skip_reply": False},
    )
    multi_round_agent, _old_round, _old_work = await _add_agent_round(
        db,
        work_id=10,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="multi-round",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=old_snapshot,
        posts=[("old-stage", "old round preview")],
    )
    await _add_agent_round(
        db,
        work_id=11,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="multi-round",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        subagent=multi_round_agent,
        active=True,
        posts=[("new-stage", "new round preview")],
    )
    await db.commit()

    data = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert data["profile_id"] == 1
    assert data["has_more_recent"] is False
    assert {entry["work_id"] for entry in data["active"]} == {2, 3, 4, 5, 11}
    assert {entry["work_id"] for entry in data["recent"]} == {6, 7}

    active_by_name = {entry["name"]: entry for entry in data["active"]}
    assert active_by_name["queued"]["status"] == "ready_for_llm"
    assert active_by_name["running"]["status"] == "running"
    assert active_by_name["waiting"]["status"] == "waiting_external_work"
    assert active_by_name["expired"]["status"] == "execution_unknown"
    assert all(entry["completed"] is False for entry in data["active"])
    assert active_by_name["multi-round"]["work_id"] == 11
    assert active_by_name["multi-round"]["stage"] == "new-stage"
    assert active_by_name["multi-round"]["preview"] == "new round preview"
    assert all(entry["stage"] != "old-stage" for entry in data["active"])
    assert [entry["work_id"] for entry in data["recent"]] == [7, 6]


@pytest.mark.asyncio
async def test_board_classifies_terminal_work_without_round_results_and_hides_pending_confirmation(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    session_id = "board-api-session"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id=session_id, uid=uid, profile_id=1, profile_override_id=1)

    terminal_specs = (
        (2, "succeeded-without-round-result", SessionReplyWorkStatus.SUCCEEDED, {"response": {"skip_reply": True}}),
        (3, "failed-without-round-result", SessionReplyWorkStatus.FAILED, None),
        (4, "cancelled-without-round-result", SessionReplyWorkStatus.CANCELLED, None),
    )
    for work_id, name, status, execution_state in terminal_specs:
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            name=name,
            status=status,
            execution_state=execution_state,
        )

    terminal_auto_watch_agent, _, _ = await _add_agent_round(
        db,
        work_id=6,
        uid=uid,
        session_id=session_id,
        profile_id=1,
        name="terminal-auto-watch-no-result",
        status=SessionReplyWorkStatus.SUCCEEDED,
    )

    pending_agent, pending_round, pending_work = await _add_agent_round(
        db,
        work_id=5,
        uid=uid,
        session_id=session_id,
        profile_id=1,
        name="pending-confirmation",
        status=SessionReplyWorkStatus.SUCCEEDED,
        active=True,
    )
    pending_round.waiting_confirmation = True

    source_message = Message(
        session_id=session_id,
        uid=uid,
        profile_id=1,
        work_id=pending_work.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TOOL_CALL,
        content_format=MessageContentFormat.TEXT,
        content="{}",
    )
    db.add(source_message)
    await db.flush()
    record = AuditRecord(
        uid=uid,
        operator_username=uid,
        session_id=session_id,
        source="interactive_tool",
        language="en",
        status=AuditRecordStatus.PENDING,
        source_work_id=pending_work.id,
        source_assistant_message_id=source_message.id,
        working_directory=".",
        round_arguments_hash="board-confirmation-hash",
        tool_count=1,
    )
    db.add(record)
    await db.flush()
    confirmation_marker = "board-pending-confirmation-body"
    confirmation_message = Message(
        session_id=session_id,
        uid=uid,
        profile_id=1,
        work_id=pending_work.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.AUDIT_CONFIRMATION,
        content_format=MessageContentFormat.TEXT,
        content=json.dumps({"audit_record_id": record.id, "test_marker": confirmation_marker}),
    )
    db.add(confirmation_message)
    await db.flush()
    pending_work.result_message_id = confirmation_message.id
    await db.commit()

    data = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": session_id}))
    assert {entry["name"] for entry in data["recent"]} == {
        "succeeded-without-round-result",
        "failed-without-round-result",
        "cancelled-without-round-result",
        "terminal-auto-watch-no-result",
    }
    assert [entry["name"] for entry in data["active"]] == ["pending-confirmation"]

    recent_by_name = {entry["name"]: entry for entry in data["recent"]}
    assert recent_by_name["succeeded-without-round-result"]["completed"] is True
    assert recent_by_name["succeeded-without-round-result"]["status"] == "succeeded"
    assert recent_by_name["failed-without-round-result"]["completed"] is True
    assert recent_by_name["failed-without-round-result"]["status"] == "failed"
    assert recent_by_name["cancelled-without-round-result"]["completed"] is True
    assert recent_by_name["cancelled-without-round-result"]["status"] == "cancelled"
    assert recent_by_name["terminal-auto-watch-no-result"]["completed"] is True
    assert recent_by_name["terminal-auto-watch-no-result"]["status"] == "execution_unknown"
    assert data["active"][0]["completed"] is False
    assert data["active"][0]["status"] is None

    selected = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": session_id, "selected_subagent_id": pending_agent.id},
        )
    )
    assert selected["selected"]["status"] is None

    detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{pending_agent.id}",
            params={"session_id": session_id},
        )
    )
    assert detail["completed"] is False
    assert detail["status"] is None
    serialized_detail = json.dumps(detail)
    assert "audit_record_id" not in serialized_detail
    assert "source_assistant_message_id" not in serialized_detail
    assert confirmation_marker not in serialized_detail

    terminal_auto_watch_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{terminal_auto_watch_agent.id}",
            params={"session_id": session_id},
        )
    )
    assert terminal_auto_watch_detail["completed"] is True
    assert terminal_auto_watch_detail["status"] == "execution_unknown"


@pytest.mark.asyncio
async def test_board_list_recent_limit_and_active_rows_are_independent(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    for index in range(12):
        work_id = index + 2
        result = _snapshot(
            work_id,
            status="succeeded",
            result={"content": f"completed-{work_id}", "files": None, "skip_reply": False},
        )
        created_at = datetime(2026, 1, 1)
        if work_id == 2:
            updated_at = created_at + timedelta(seconds=2.5)
            posts = [("progress", "post-2-progress"), ("succeeded", "post-2-final")]
        elif work_id == 3:
            updated_at = created_at + timedelta(seconds=1.5)
            posts = [
                ("progress", "post-3-progress-1"),
                ("progress", "post-3-progress-2"),
                ("progress", "post-3-progress-3"),
                ("succeeded", "post-3-final"),
            ]
        else:
            updated_at = created_at + timedelta(milliseconds=index)
            posts = None
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=f"completed-{work_id}",
            status=SessionReplyWorkStatus.SUCCEEDED,
            round_result=result,
            created_at=created_at,
            updated_at=updated_at,
            posts=posts,
        )
    active_ids = set(range(20, 45))
    non_queued_ids = set(range(40, 45))
    for work_id in sorted(active_ids):
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=f"active-{work_id}",
            status=(SessionReplyWorkStatus.RUNNING if work_id in non_queued_ids else SessionReplyWorkStatus.READY_FOR_LLM),
            active=True,
        )
    await db.commit()

    default_data = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert len(default_data["recent"]) == 10
    assert default_data["has_more_recent"] is True
    assert default_data["active_page"] == 1
    assert default_data["active_page_size"] == 20
    assert default_data["active_total"] == len(active_ids)
    first_page_ids = [entry["work_id"] for entry in default_data["active"]]
    assert first_page_ids == list(range(40, 45)) + list(range(20, 35))

    second_page_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "board-api-session", "recent_limit": 1, "active_page": 2},
        )
    )
    second_page_ids = [entry["work_id"] for entry in second_page_data["active"]]
    assert second_page_data["active_page"] == 2
    assert second_page_data["active_page_size"] == 20
    assert second_page_data["active_total"] == len(active_ids)
    assert second_page_ids == list(range(35, 40))
    assert set(first_page_ids).isdisjoint(second_page_ids)
    assert len(second_page_data["recent"]) == 1
    assert [entry["work_id"] for entry in second_page_data["recent"]] == [3]
    assert second_page_data["has_more_recent"] is True

    selected_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={
                "session_id": "board-api-session",
                "active_page": 1,
                "selected_subagent_id": "board-api-agent-39",
            },
        )
    )
    assert 39 not in [entry["work_id"] for entry in selected_data["active"]]
    assert selected_data["selected"]["subagent_id"] == "board-api-agent-39"
    assert selected_data["selected"]["work_id"] == 39

    clamped_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "board-api-session", "active_page": 999},
        )
    )
    assert clamped_data["active_page"] == 2
    assert [entry["work_id"] for entry in clamped_data["active"]] == second_page_ids
    assert clamped_data["active_total"] == len(active_ids)

    expanded_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "board-api-session", "recent_limit": 20},
        )
    )
    assert len(expanded_data["recent"]) == 12
    assert expanded_data["has_more_recent"] is False
    assert [entry["work_id"] for entry in expanded_data["recent"][:2]] == [3, 2]
    assert expanded_data["active_total"] == len(active_ids)


@pytest.mark.asyncio
async def test_board_scope_permissions_and_owner_profile_selection(api_context) -> None:
    db, auth_state, client = api_context
    owner_uid = "board-api-owner"
    admin_uid = "board-api-admin"
    other_uid = "board-api-other"
    no_profile_uid = "board-api-no-profile"
    await _add_user(db, owner_uid)
    await _add_user(db, admin_uid, is_superuser=True)
    await _add_user(db, other_uid)
    await _add_user(db, no_profile_uid)
    await _add_profile(db, uid=owner_uid, profile_id=1, name="owner-default", is_default=True)
    await _add_profile(db, uid=owner_uid, profile_id=2, name="owner-session-profile")
    await _add_profile(db, uid=admin_uid, profile_id=3, name="admin-default", is_default=True)
    await _add_profile(db, uid=other_uid, profile_id=4, name="other-default", is_default=True)
    await _add_session(
        db,
        session_id="owner-target-session",
        uid=owner_uid,
        profile_id=2,
        profile_override_id=2,
    )
    await _add_session(
        db,
        session_id="owner-other-session",
        uid=owner_uid,
        profile_id=1,
        profile_override_id=1,
    )
    await _add_session(
        db,
        session_id="other-user-session",
        uid=other_uid,
        profile_id=4,
        profile_override_id=4,
    )
    await _add_session(db, session_id="no-profile-session", uid=no_profile_uid)

    await _add_agent_round(
        db,
        work_id=10,
        uid=owner_uid,
        session_id="owner-target-session",
        profile_id=2,
        name="target-profile-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    await _add_agent_round(
        db,
        work_id=11,
        uid=owner_uid,
        session_id="owner-target-session",
        profile_id=1,
        name="target-other-profile-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    await _add_agent_round(
        db,
        work_id=12,
        uid=owner_uid,
        session_id="owner-other-session",
        profile_id=1,
        name="other-session-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    other_agent, _other_round, _other_work = await _add_agent_round(
        db,
        work_id=13,
        uid=other_uid,
        session_id="other-user-session",
        profile_id=4,
        name="other-user-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    await db.commit()

    owner_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "owner-target-session"},
        )
    )
    assert owner_data["profile_id"] == 2
    assert [entry["work_id"] for entry in owner_data["active"]] == [10]
    _error(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "other-user-session"},
        ),
        403,
    )

    auth_state.update(uid=admin_uid, is_superuser=True)
    admin_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "owner-target-session"},
        )
    )
    assert admin_data["profile_id"] == 2
    assert [entry["work_id"] for entry in admin_data["active"]] == [10]
    other_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "other-user-session"},
        )
    )
    assert other_data["profile_id"] == 4
    assert [entry["work_id"] for entry in other_data["active"]] == [other_agent.latest_child_work_id]

    auth_state.update(uid=no_profile_uid, is_superuser=False)
    no_profile_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "no-profile-session"},
        )
    )
    assert no_profile_data == {
        "active": [],
        "active_page": 1,
        "active_page_size": 20,
        "active_total": 0,
        "recent": [],
        "has_more_recent": False,
        "profile_id": None,
        "selected": None,
    }


@pytest.mark.asyncio
async def test_board_detail_selects_round_hides_terminal_posts_and_projects_result(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    old_result = _snapshot(
        2,
        status="succeeded",
        result={"content": "old result", "files": None, "skip_reply": False},
    )
    agent, _old_round, old_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="history-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=old_result,
        posts=[("plan", "old visible"), ("succeeded", "old terminal")],
    )
    latest_result = _snapshot(
        3,
        status="succeeded",
        result={
            "content": "latest result",
            "files": [{"name": "private.txt", "path": "/internal/private.txt"}],
            "skip_reply": False,
        },
    )
    await _add_agent_round(
        db,
        work_id=3,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="history-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        subagent=agent,
        round_result=latest_result,
        posts=[
            ("planning", "latest visible"),
            ("succeeded", "latest terminal"),
            ("progress", json.dumps({"full_result_operation": "query_result"})),
        ],
    )
    await db.commit()

    latest = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert latest["work_id"] == 3
    assert latest["status"] == "succeeded"
    assert latest["completed"] is True
    assert [round_data["work_id"] for round_data in latest["rounds"]] == [3, 2]
    assert [(post["stage"], post["content"]) for post in latest["posts"]] == [("planning", "latest visible")]
    assert latest["result_text"] == "latest result"
    assert latest["reason"] is None
    assert latest["ended_without_reply"] is False
    assert "files" not in latest
    assert "/internal/private.txt" not in json.dumps(latest)

    historical = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session", "work_id": old_work.id},
        )
    )
    assert historical["work_id"] == old_work.id
    assert historical["result_text"] == "old result"
    assert [(post["stage"], post["content"]) for post in historical["posts"]] == [("plan", "old visible")]


@pytest.mark.asyncio
async def test_board_detail_paginates_rounds_and_keeps_work_selection(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="round-pagination-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=_snapshot(
            2,
            status="succeeded",
            result={"content": "round-2", "files": None, "skip_reply": False},
        ),
    )
    for work_id in range(3, 25):
        await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name="round-pagination-agent",
            status=SessionReplyWorkStatus.SUCCEEDED,
            subagent=agent,
            round_result=_snapshot(
                work_id,
                status="succeeded",
                result={"content": f"round-{work_id}", "files": None, "skip_reply": False},
            ),
        )
    await db.commit()

    first = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    first_round_ids = [round_data["work_id"] for round_data in first["rounds"]]
    assert len(first_round_ids) == 20
    assert first_round_ids == sorted(first_round_ids, reverse=True)
    assert first_round_ids == list(range(24, 4, -1))
    assert first["next_before_round_id"] == first_round_ids[-1]

    second = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={
                "session_id": "board-api-session",
                "before_round_id": first["next_before_round_id"],
            },
        )
    )
    assert set(second) == {"rounds", "next_before_round_id"}
    second_round_ids = [round_data["work_id"] for round_data in second["rounds"]]
    assert second_round_ids == [4, 3, 2]
    assert second["next_before_round_id"] is None
    assert set(first_round_ids).isdisjoint(second_round_ids)
    assert first_round_ids + second_round_ids == list(range(24, 1, -1))

    historical = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session", "work_id": second_round_ids[-1]},
        )
    )
    assert historical["work_id"] == 2
    assert historical["result_text"] == "round-2"


@pytest.mark.asyncio
async def test_board_detail_returns_latest_fifty_posts_then_earlier_page_without_duplicates(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    long_content = "多字节正文" * 300
    agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="pagination-agent",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
        posts=[
            ("progress", "post-0"),
            ("progress", "post-1"),
            ("succeeded", "hidden-boundary"),
            *[("progress", f"post-{index}") for index in range(2, 51)],
            ("progress", long_content),
        ],
    )
    await db.commit()

    first = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert len(first["posts"]) == 50
    assert [post["content"] for post in first["posts"]] == [f"post-{index}" for index in range(2, 51)] + [long_content[:1024]]
    assert all(post["truncated"] is False for post in first["posts"][:-1])
    assert len(first["posts"][-1]["content"]) == 1024
    assert first["posts"][-1]["truncated"] is True
    assert first["next_before_post_id"] == first["posts"][0]["id"]

    second = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={
                "session_id": "board-api-session",
                "before_post_id": first["next_before_post_id"],
            },
        )
    )
    assert second["next_before_post_id"] is None
    assert [post["content"] for post in second["posts"]] == ["post-0", "post-1"]
    assert all(post["truncated"] is False for post in second["posts"])
    assert all(post["stage"] == "progress" for post in first["posts"] + second["posts"])
    assert {post["id"] for post in first["posts"]}.isdisjoint({post["id"] for post in second["posts"]})
    assert {post["content"] for post in first["posts"] + second["posts"]} == {f"post-{index}" for index in range(51)} | {long_content[:1024]}


@pytest.mark.asyncio
async def test_board_detail_projects_success_failure_and_silent_results_without_files(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)

    long_result = "多字节结果" * 2000
    success = _snapshot(
        2,
        status="succeeded",
        result={
            "content": long_result,
            "files": [{"path": "C:\\server\\secret.bin"}],
            "skip_reply": False,
        },
    )
    failure = _snapshot(3, status="failed", error="real failure reason")
    silent = _snapshot(
        4,
        status="succeeded",
        result={"content": None, "files": [{"path": "/srv/internal/hidden.bin"}], "skip_reply": True},
    )
    agents = {}
    for work_id, name, snapshot in ((2, "success-agent", success), (3, "failure-agent", failure), (4, "silent-agent", silent)):
        agent, _round_row, _work = await _add_agent_round(
            db,
            work_id=work_id,
            uid=uid,
            session_id="board-api-session",
            profile_id=1,
            name=name,
            status=SessionReplyWorkStatus.SUCCEEDED if snapshot["status"] == "succeeded" else SessionReplyWorkStatus.FAILED,
            round_result=snapshot,
        )
        agents[name] = agent

    no_file_silent = _snapshot(
        5,
        status="succeeded",
        result={"content": None, "files": None, "skip_reply": True},
    )
    no_file_silent_agent, _round_row, _work = await _add_agent_round(
        db,
        work_id=5,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="silent-no-files-agent",
        status=SessionReplyWorkStatus.SUCCEEDED,
        round_result=no_file_silent,
        posts=[("planning", "visible before silent"), ("ended_without_reply", "")],
    )
    agents["silent-no-files-agent"] = no_file_silent_agent
    await db.commit()

    list_data = _ok(
        await client.get(
            "/api/v1/chat/sessions/subagents",
            params={"session_id": "board-api-session"},
        )
    )
    no_file_silent_list = next(entry for entry in list_data["recent"] if entry["name"] == "silent-no-files-agent")
    assert no_file_silent_list["stage"] is None
    assert no_file_silent_list["preview"] == ""

    success_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['success-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert success_data["result_text"] == long_result[:8192]
    assert len(success_data["result_text"]) == 8192
    assert success_data["result_text_truncated"] is True
    assert success_data["reason"] is None
    assert success_data["reason_truncated"] is False
    assert success_data["ended_without_reply"] is False

    failure_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['failure-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert failure_data["status"] == "failed"
    assert failure_data["result_text"] is None
    assert failure_data["result_text_truncated"] is False
    assert failure_data["reason"] == "real failure reason"
    assert failure_data["reason_truncated"] is False
    assert failure_data["ended_without_reply"] is False

    silent_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['silent-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert silent_data["status"] == "succeeded"
    assert silent_data["result_text"] is None
    assert silent_data["result_text_truncated"] is False
    assert silent_data["reason"] is None
    assert silent_data["reason_truncated"] is False
    assert silent_data["ended_without_reply"] is True

    no_file_silent_data = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agents['silent-no-files-agent'].id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert no_file_silent_data["status"] == "succeeded"
    assert no_file_silent_data["result_text"] is None
    assert no_file_silent_data["result_text_truncated"] is False
    assert no_file_silent_data["reason"] is None
    assert no_file_silent_data["reason_truncated"] is False
    assert no_file_silent_data["ended_without_reply"] is True
    assert [(post["stage"], post["content"]) for post in no_file_silent_data["posts"]] == [("planning", "visible before silent")]

    serialized = json.dumps({"success": success_data, "silent": silent_data})
    assert "files" not in serialized
    assert "secret.bin" not in serialized
    assert "hidden.bin" not in serialized


@pytest.mark.asyncio
async def test_board_reads_are_repeatable_and_do_not_mark_tool_delivery_or_touch_round(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    parent = await _add_parent_work(
        db,
        work_id=1,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
    )
    agent, round_row, child_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="read-only-agent",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
        posts=[("plan", "read-only preview")],
    )
    call = SubagentToolCall(
        subagent_id=agent.id,
        child_work_id=child_work.id,
        parent_work_id=parent.id,
        tool_call_id="board-api-tool-call",
        arguments={"operation": "create", "name": agent.name},
        delivered=False,
    )
    db.add(call)
    await db.commit()
    before_last_called_at = agent.last_called_at
    before_status = child_work.status
    before_execution_state = deepcopy(child_work.execution_state)
    before_round = {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": deepcopy(round_row.result),
    }

    first_list = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    second_list = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    first_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    second_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert first_list == second_list
    assert first_detail == second_detail

    await db.refresh(agent)
    await db.refresh(child_work)
    await db.refresh(round_row)
    await db.refresh(call)
    assert agent.last_called_at == before_last_called_at
    assert call.delivered is False
    assert child_work.status == before_status
    assert child_work.execution_state == before_execution_state
    assert {
        "current_work_id": round_row.current_work_id,
        "waiting_confirmation": round_row.waiting_confirmation,
        "result": round_row.result,
    } == before_round


@pytest.mark.asyncio
async def test_board_rejects_out_of_scope_agents_work_ids_and_invalid_pagination(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_profile(db, uid=uid, profile_id=2, name="other-profile")
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    parent = await _add_parent_work(
        db,
        work_id=1,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
    )
    target_agent, _target_round, target_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="target-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    out_of_scope_agent, _other_round, other_work = await _add_agent_round(
        db,
        work_id=3,
        uid=uid,
        session_id="board-api-session",
        profile_id=2,
        name="other-profile-agent",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    await db.commit()

    _error(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{out_of_scope_agent.id}",
            params={"session_id": "board-api-session"},
        ),
        400,
    )
    for invalid_work_id in (parent.id, other_work.id, 999999):
        _error(
            await client.get(
                f"/api/v1/chat/sessions/subagents/{target_agent.id}",
                params={"session_id": "board-api-session", "work_id": invalid_work_id},
            ),
            400,
        )
    assert target_work.id == 2
    for recent_limit in (0, 101):
        _error(
            await client.get(
                "/api/v1/chat/sessions/subagents",
                params={"session_id": "board-api-session", "recent_limit": recent_limit},
            ),
            422,
        )
    for parameter in ("work_id", "before_post_id"):
        for invalid_value in (0, -1):
            _error(
                await client.get(
                    f"/api/v1/chat/sessions/subagents/{target_agent.id}",
                    params={"session_id": "board-api-session", parameter: invalid_value},
                ),
                422,
            )


@pytest.mark.asyncio
async def test_board_reads_reflect_direct_running_to_success_and_failure_updates(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    await _add_user(db, uid)
    await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    await _add_session(db, session_id="board-api-session", uid=uid, profile_id=1, profile_override_id=1)
    success_agent, success_round, success_work = await _add_agent_round(
        db,
        work_id=2,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="updated-success",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
    )
    failure_agent, failure_round, failure_work = await _add_agent_round(
        db,
        work_id=3,
        uid=uid,
        session_id="board-api-session",
        profile_id=1,
        name="updated-failure",
        status=SessionReplyWorkStatus.RUNNING,
        active=True,
    )
    await db.commit()

    initial = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert {entry["work_id"] for entry in initial["active"]} == {2, 3}
    assert initial["recent"] == []

    success_round.result = _snapshot(
        2,
        status="succeeded",
        result={"content": "persisted success", "files": None, "skip_reply": False},
    )
    success_work.status = SessionReplyWorkStatus.SUCCEEDED
    success_work.locked_by = None
    success_work.lock_until = None
    success_work.execution_state = {
        "child_work_id": 2,
        "response": {"content": "persisted success", "files": None, "skip_reply": False},
    }
    success_work.updated_at = datetime(2026, 3, 1, 0, 0, 2)
    success_agent.active_child_work_id = None

    failure_round.result = _snapshot(3, status="failed", error="persisted failure")
    failure_work.status = SessionReplyWorkStatus.FAILED
    failure_work.locked_by = None
    failure_work.lock_until = None
    failure_work.error = "persisted failure"
    failure_work.execution_state = {"child_work_id": 3}
    failure_work.updated_at = datetime(2026, 3, 1, 0, 0, 3)
    failure_agent.active_child_work_id = None
    db.add_all([success_agent, success_round, success_work, failure_agent, failure_round, failure_work])
    await db.commit()

    updated = _ok(await client.get("/api/v1/chat/sessions/subagents", params={"session_id": "board-api-session"}))
    assert updated["active"] == []
    assert {entry["work_id"] for entry in updated["recent"]} == {2, 3}
    updated_by_work = {entry["work_id"]: entry for entry in updated["recent"]}
    assert updated_by_work[2]["status"] == "succeeded"
    assert updated_by_work[2]["completed"] is True
    assert updated_by_work[3]["status"] == "failed"
    assert updated_by_work[3]["completed"] is True

    success_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{success_agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    failure_detail = _ok(
        await client.get(
            f"/api/v1/chat/sessions/subagents/{failure_agent.id}",
            params={"session_id": "board-api-session"},
        )
    )
    assert success_detail["result_text"] == "persisted success"
    assert success_detail["reason"] is None
    assert failure_detail["result_text"] is None
    assert failure_detail["reason"] == "persisted failure"
