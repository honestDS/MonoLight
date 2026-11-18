import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.chat import router
from app.core.crud.session.subagent import subagent_crud
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.message import Message, MessageContentFormat, MessageRole, MessageType
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentWork
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


async def _add_board_version(
    db: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    name: str,
) -> Subagent:
    subagent, _round, _work = await _add_agent_round(
        db,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        name=name,
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        active=True,
    )
    await subagent_crud.create_board_post(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        subagent_id=subagent.id,
        child_work_id=work_id,
        stage="progress",
        content=f"{name} progress",
    )
    return subagent


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
    assert 0 < data["lease_recheck_in_ms"] <= 300_000


@pytest.mark.asyncio
async def test_activity_tracks_board_profile_selection_without_new_progress(api_context) -> None:
    db, _auth_state, client = api_context
    uid = "board-api-owner"
    platform_session_id = "board-api-platform-session"
    default_session_id = "board-api-default-session"
    control_session_id = "board-api-control-session"

    await _add_user(db, uid)
    default_profile = await _add_profile(db, uid=uid, profile_id=1, name="owner-default", is_default=True)
    platform_profile = await _add_profile(db, uid=uid, profile_id=2, name="owner-platform")
    override_profile = await _add_profile(db, uid=uid, profile_id=3, name="owner-override")

    platform = MessagePlatform(
        name="owner-weixin",
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        uid=uid,
        profile_id=platform_profile.id,
    )
    db.add(platform)
    await db.flush()

    platform_session = await _add_session(db, session_id=platform_session_id, uid=uid)
    platform_session.message_platform_id = platform.id
    await _add_session(db, session_id=default_session_id, uid=uid)
    await _add_session(db, session_id=control_session_id, uid=uid)

    await _add_board_version(
        db,
        work_id=20,
        uid=uid,
        session_id=platform_session_id,
        profile_id=platform_profile.id,
        name="platform-agent",
    )
    await _add_board_version(
        db,
        work_id=21,
        uid=uid,
        session_id=default_session_id,
        profile_id=default_profile.id,
        name="default-agent",
    )
    await db.commit()

    async def assert_activity_profiles(expected: dict[str, int]) -> dict[str, Any]:
        activity = _ok(await client.get("/api/v1/chat/sessions/activity"))
        activity_by_session = {entry["session_id"]: entry for entry in activity}
        for session_id, expected_profile_id in expected.items():
            board = _ok(
                await client.get(
                    "/api/v1/chat/sessions/subagents",
                    params={"session_id": session_id},
                )
            )
            assert board["profile_id"] == expected_profile_id
            assert activity_by_session[session_id]["subagent_board_profile_id"] == board["profile_id"]
        assert activity_by_session[control_session_id]["subagent_board_profile_id"] is None
        assert activity_by_session[control_session_id]["subagent_board_revision"] == 0
        return activity_by_session

    expected_profiles = {
        platform_session_id: platform_profile.id,
        default_session_id: default_profile.id,
    }
    activity_by_session = await assert_activity_profiles(expected_profiles)
    initial_revisions = {
        platform_session_id: activity_by_session[platform_session_id]["subagent_board_revision"],
        default_session_id: activity_by_session[default_session_id]["subagent_board_revision"],
    }
    assert initial_revisions == {platform_session_id: 1, default_session_id: 1}

    def assert_work_revisions(activity_rows: dict[str, Any]) -> None:
        assert {
            platform_session_id: activity_rows[platform_session_id]["subagent_board_revision"],
            default_session_id: activity_rows[default_session_id]["subagent_board_revision"],
        } == initial_revisions

    default_profile.is_default = False
    override_profile.is_default = True
    await db.commit()
    activity_by_session = await assert_activity_profiles(
        {
            platform_session_id: platform_profile.id,
            default_session_id: override_profile.id,
        }
    )
    assert_work_revisions(activity_by_session)

    platform.profile_id = None
    await db.commit()
    activity_by_session = await assert_activity_profiles(
        {
            platform_session_id: override_profile.id,
            default_session_id: override_profile.id,
        }
    )
    assert_work_revisions(activity_by_session)

    platform_session.profile_override_id = default_profile.id
    await db.commit()
    activity_by_session = await assert_activity_profiles(
        {
            platform_session_id: default_profile.id,
            default_session_id: override_profile.id,
        }
    )
    assert_work_revisions(activity_by_session)


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
        "lease_recheck_in_ms": None,
    }


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
