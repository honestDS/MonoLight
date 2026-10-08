from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from test_session_activity_api import (
    _add_work,
    _make_session,
    _persist_message,
    api_context,
    db_session,
)

from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.message import MessageRole
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

# 复用活动接口测试中的真实 SQLite AsyncSession/httpx fixture。
__all__ = ("api_context", "db_session")

TASK_FIELDS = {
    "session_id",
    "title",
    "source",
    "latest_message_id",
    "last_active",
    "last_read_message_id",
    "last_read_at",
    "is_running",
    "completed_message_id",
    "completed_status",
    "has_unread_result",
}
ACTIVITY_FIELDS = TASK_FIELDS | {"is_owned", "is_loading", "is_reply_running"}
INTERACTIVE_WORK_TYPES = (
    SessionReplyWorkType.FOREGROUND_REPLY,
    SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
)


def _task_rows(activity_rows: list[dict]) -> list[dict]:
    return [row for row in activity_rows if row["is_owned"] and (row["is_running"] or row["has_unread_result"])]


ACTIVE_WORK_STATUSES = tuple(SESSION_REPLY_ACTIVE_STATUSES)
ACTIVE_WORK_CASES = tuple((work_type, status) for work_type in SessionReplyWorkType for status in ACTIVE_WORK_STATUSES)
NON_COMPLETING_WORK_STATUSES = (
    SessionReplyWorkStatus.MERGED,
    SessionReplyWorkStatus.CANCELLED,
)
COMPLETED_WORK_CASES = (
    SessionReplyWorkStatus.SUCCEEDED,
    SessionReplyWorkStatus.FAILED,
)
BACKGROUND_TASK_CASES = (
    (BackgroundTaskStatus.PENDING, False, BackgroundTaskReplyStatus.NONE, True),
    (BackgroundTaskStatus.RUNNING, False, BackgroundTaskReplyStatus.NONE, True),
    (BackgroundTaskStatus.SUCCEEDED, True, BackgroundTaskReplyStatus.PENDING, True),
    (BackgroundTaskStatus.FAILED, True, BackgroundTaskReplyStatus.RUNNING, True),
    (BackgroundTaskStatus.CANCELLED, True, BackgroundTaskReplyStatus.PENDING, False),
)


async def _add_result_work(
    db,
    *,
    session_id: str,
    uid: str,
    status: SessionReplyWorkStatus,
    sequence_no: int,
    created_at: datetime,
    content: str = "result",
):
    work = await _add_work(
        db,
        session_id=session_id,
        uid=uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=status,
        sequence_no=sequence_no,
    )
    result_message = await _persist_message(
        db,
        session_id=session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        content=content,
        created_at=created_at,
    )
    await db.commit()
    work.result_message_id = result_message.id
    await db.commit()
    return work, result_message


async def _add_background_task(
    db,
    *,
    session_id: str,
    uid: str,
    status: BackgroundTaskStatus,
    auto_reply: bool,
    reply_status: BackgroundTaskReplyStatus,
):
    task = BackgroundTask(
        uid=uid,
        session_id=session_id,
        profile_id=1,
        tool_call_id="tool-call",
        tool_name="tool",
        status=status,
        auto_reply=auto_reply,
        reply_status=reply_status,
    )
    db.add(task)
    await db.commit()
    return task


@pytest.mark.asyncio
async def test_tasks_are_owned_only_nonempty_and_lightweight(api_context) -> None:
    db, auth_state, client = api_context
    base = datetime(2026, 8, 1, 1, 2, 3)
    owned_task = _make_session("owned-task", "user-1", created_at=base, source="")
    owned_empty = _make_session("owned-empty", "user-1", created_at=base + timedelta(seconds=1))
    owned_user_only = _make_session("owned-user-only", "user-1", created_at=base + timedelta(seconds=2))
    other_task = _make_session("other-task", "user-2", created_at=base + timedelta(seconds=3))
    owned_task.title = "Owned task"
    db.add_all([owned_task, owned_empty, owned_user_only, other_task])
    await db.commit()

    await _persist_message(
        db,
        session_id=owned_user_only.session_id,
        uid=owned_user_only.uid,
        created_at=base + timedelta(seconds=4),
    )
    await db.commit()
    await _add_work(
        db,
        session_id=owned_task.session_id,
        uid=owned_task.uid,
        work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        status=SessionReplyWorkStatus.READY_FOR_LLM,
    )
    await _add_work(
        db,
        session_id=other_task.session_id,
        uid=other_task.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
    )
    _other_result_work, other_result = await _add_result_work(
        db,
        session_id=other_task.session_id,
        uid=other_task.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=2,
        created_at=base + timedelta(seconds=5),
        content="other result",
    )
    other_task.last_read_message_id = other_result.id
    other_task.last_read_at = base + timedelta(seconds=6)
    await db.commit()

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert {row["session_id"] for row in activity_rows} == {"owned-task", "owned-empty", "owned-user-only"}
    assert all(set(row) == ACTIVITY_FIELDS for row in activity_rows)
    rows = {row["session_id"]: row for row in activity_rows}
    assert rows["owned-task"] == {
        "session_id": "owned-task",
        "title": "Owned task",
        "source": "http",
        "latest_message_id": None,
        "last_active": "2026-08-01 01:02:03",
        "last_read_message_id": None,
        "last_read_at": None,
        "is_running": True,
        "completed_message_id": None,
        "completed_status": None,
        "has_unread_result": False,
        "is_owned": True,
        "is_loading": True,
        "is_reply_running": False,
    }
    assert rows["owned-empty"]["is_running"] is False
    assert rows["owned-empty"]["has_unread_result"] is False
    assert rows["owned-user-only"]["is_running"] is False
    assert rows["owned-user-only"]["has_unread_result"] is False
    assert [row["session_id"] for row in _task_rows(activity_rows)] == ["owned-task"]

    removed_response = await client.get("/api/v1/chat/sessions/tasks")
    assert removed_response.status_code == 404

    auth_state["is_superuser"] = True
    admin_response = await client.get("/api/v1/chat/sessions/activity")
    assert admin_response.status_code == 200
    admin_activity_rows = admin_response.json()["data"]
    assert {row["session_id"] for row in admin_activity_rows} == {"owned-task", "owned-empty", "owned-user-only", "other-task"}
    assert all(set(row) == ACTIVITY_FIELDS for row in admin_activity_rows)
    assert [row["session_id"] for row in _task_rows(admin_activity_rows)] == ["owned-task"]
    other_row = {row["session_id"]: row for row in admin_activity_rows}["other-task"]
    assert other_row["is_owned"] is False
    assert other_row["is_loading"] is True
    assert other_row["is_reply_running"] is True
    assert other_row["is_running"] is False
    assert other_row["has_unread_result"] is False
    assert other_row["last_read_message_id"] is None
    assert other_row["last_read_at"] is None
    assert other_row["completed_message_id"] is None
    assert other_row["completed_status"] is None

    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = statement.strip()
        if normalized.upper().startswith("SELECT"):
            statements.append(normalized)

    sync_engine = db.sync_session.bind
    assert sync_engine is not None
    event.listen(sync_engine, "before_cursor_execute", capture_sql)
    try:
        lightweight_response = await client.get("/api/v1/chat/sessions/activity")
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture_sql)

    assert lightweight_response.status_code == 200
    assert len(statements) == 1
    sql = statements[0].lower()
    assert "message.content" not in sql
    assert "message.reasoning_content" not in sql
    assert "message.provider_metadata" not in sql
    assert "execution_state" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_type", "status"),
    ACTIVE_WORK_CASES,
    ids=[f"{work_type.value}-{status.value}" for work_type, status in ACTIVE_WORK_CASES],
)
async def test_all_work_types_and_active_statuses_keep_sessions_running(api_context, work_type, status) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        f"active-{work_type.value}-{status.value}",
        "user-1",
        created_at=datetime(2026, 8, 2, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=work_type,
        status=status,
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert [row["session_id"] for row in activity_rows] == [session.session_id]
    assert set(activity_rows[0]) == ACTIVITY_FIELDS
    assert activity_rows[0]["is_owned"] is True
    assert activity_rows[0]["is_loading"] is True
    assert activity_rows[0]["is_reply_running"] is (work_type in INTERACTIVE_WORK_TYPES)
    task_rows = _task_rows(activity_rows)
    assert len(task_rows) == 1
    assert task_rows[0]["is_running"] is True
    assert task_rows[0]["completed_message_id"] is None
    assert task_rows[0]["completed_status"] is None
    assert task_rows[0]["has_unread_result"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NON_COMPLETING_WORK_STATUSES, ids=lambda status: status.value)
async def test_merged_and_cancelled_work_are_not_completed(api_context, status) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        f"terminal-{status.value}",
        "user-1",
        created_at=datetime(2026, 8, 3, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    _work, result_message = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=status,
        sequence_no=1,
        created_at=datetime(2026, 8, 3, 2, 3, 4),
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert len(activity_rows) == 1
    assert set(activity_rows[0]) == ACTIVITY_FIELDS
    assert _task_rows(activity_rows) == []
    assert activity_rows[0]["is_running"] is False
    assert activity_rows[0]["completed_message_id"] is None
    assert activity_rows[0]["completed_status"] is None
    assert activity_rows[0]["has_unread_result"] is False
    assert result_message.id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", COMPLETED_WORK_CASES, ids=lambda status: status.value)
async def test_succeeded_and_failed_work_report_unread_result(api_context, status) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        f"completed-{status.value}",
        "user-1",
        created_at=datetime(2026, 8, 4, 1, 2, 3),
    )
    session.title = "Completed task"
    db.add(session)
    await db.commit()
    _work, result_message = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=status,
        sequence_no=1,
        created_at=datetime(2026, 8, 4, 2, 3, 4),
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert len(activity_rows) == 1
    row = activity_rows[0]
    assert set(row) == ACTIVITY_FIELDS
    assert _task_rows(activity_rows) == [row]
    assert row["session_id"] == session.session_id
    assert row["is_owned"] is True
    assert row["is_loading"] is False
    assert row["is_reply_running"] is False
    assert row["completed_message_id"] == result_message.id
    assert row["completed_status"] == status.value
    assert row["latest_message_id"] == result_message.id
    assert row["last_active"] == "2026-08-04 02:03:04"
    assert row["is_running"] is False
    assert row["has_unread_result"] is True


@pytest.mark.asyncio
async def test_read_and_deleted_results_converge_while_active_snapshots_remain(api_context) -> None:
    db, _auth_state, client = api_context
    base = datetime(2026, 8, 5, 1, 2, 3)
    read_session = _make_session("read-result", "user-1", created_at=base)
    deleted_session = _make_session("deleted-result", "user-1", created_at=base + timedelta(seconds=1))
    running_unread_session = _make_session("running-unread", "user-1", created_at=base + timedelta(seconds=2))
    mixed_session = _make_session("mixed-work", "user-1", created_at=base + timedelta(seconds=3))
    db.add_all([read_session, deleted_session, running_unread_session, mixed_session])
    await db.commit()

    _read_work, read_result = await _add_result_work(
        db,
        session_id=read_session.session_id,
        uid=read_session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=1,
        created_at=base + timedelta(minutes=1),
        content="read result",
    )
    _deleted_work, _deleted_result = await _add_result_work(
        db,
        session_id=deleted_session.session_id,
        uid=deleted_session.uid,
        status=SessionReplyWorkStatus.FAILED,
        sequence_no=1,
        created_at=base + timedelta(minutes=2),
        content="deleted result",
    )
    _running_result_work, running_result = await _add_result_work(
        db,
        session_id=running_unread_session.session_id,
        uid=running_unread_session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=1,
        created_at=base + timedelta(minutes=3),
        content="old result",
    )
    await _add_work(
        db,
        session_id=running_unread_session.session_id,
        uid=running_unread_session.uid,
        work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
        status=SessionReplyWorkStatus.RUNNING,
        sequence_no=2,
    )
    await _add_work(
        db,
        session_id=mixed_session.session_id,
        uid=mixed_session.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
        sequence_no=1,
    )
    _mixed_result_work, mixed_result = await _add_result_work(
        db,
        session_id=mixed_session.session_id,
        uid=mixed_session.uid,
        status=SessionReplyWorkStatus.FAILED,
        sequence_no=2,
        created_at=base + timedelta(minutes=4),
        content="new result",
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert {row["session_id"] for row in activity_rows} == {"read-result", "deleted-result", "running-unread", "mixed-work"}
    assert all(set(row) == ACTIVITY_FIELDS for row in activity_rows)
    assert {row["session_id"] for row in _task_rows(activity_rows)} == {"read-result", "deleted-result", "running-unread", "mixed-work"}
    rows = {row["session_id"]: row for row in activity_rows}
    assert set(rows) == {"read-result", "deleted-result", "running-unread", "mixed-work"}
    assert rows["running-unread"]["is_running"] is True
    assert rows["running-unread"]["is_loading"] is True
    assert rows["running-unread"]["is_reply_running"] is False
    assert rows["running-unread"]["completed_message_id"] == running_result.id
    assert rows["running-unread"]["completed_status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert rows["running-unread"]["has_unread_result"] is True
    assert rows["mixed-work"]["is_running"] is True
    assert rows["mixed-work"]["is_loading"] is True
    assert rows["mixed-work"]["is_reply_running"] is True
    assert rows["mixed-work"]["completed_message_id"] == mixed_result.id
    assert rows["mixed-work"]["completed_status"] == SessionReplyWorkStatus.FAILED.value

    read_session.last_read_message_id = read_result.id
    read_session.last_read_at = base + timedelta(minutes=5)
    await db.commit()
    read_response = await client.get("/api/v1/chat/sessions/activity")
    read_activity_rows = read_response.json()["data"]
    read_rows = {row["session_id"]: row for row in read_activity_rows}
    assert set(read_rows) == {"read-result", "deleted-result", "running-unread", "mixed-work"}
    assert {row["session_id"] for row in _task_rows(read_activity_rows)} == {"deleted-result", "running-unread", "mixed-work"}
    assert read_rows["read-result"]["last_read_message_id"] == read_result.id
    assert read_rows["read-result"]["last_read_at"] == "2026-08-05 01:07:03"
    assert read_rows["read-result"]["has_unread_result"] is False

    delete_response = await client.post(
        "/api/v1/chat/sessions/delete",
        params={"session_id": deleted_session.session_id},
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["code"] == 200
    deleted_response = await client.get("/api/v1/chat/sessions/activity")
    deleted_activity_rows = deleted_response.json()["data"]
    deleted_rows = {row["session_id"]: row for row in deleted_activity_rows}
    assert set(deleted_rows) == {"read-result", "running-unread", "mixed-work"}
    assert {row["session_id"] for row in _task_rows(deleted_activity_rows)} == {"running-unread", "mixed-work"}
    assert "deleted-result" not in deleted_rows


@pytest.mark.asyncio
async def test_multiple_work_items_return_one_row_using_latest_result_id_status(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        "multiple-work",
        "user-1",
        created_at=datetime(2026, 8, 6, 1, 2, 3),
    )
    db.add(session)
    await db.commit()

    _first_work, first_result = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=2,
        created_at=datetime(2026, 8, 6, 2, 3, 4),
        content="first result",
    )
    _second_work, second_result = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=SessionReplyWorkStatus.FAILED,
        sequence_no=1,
        created_at=datetime(2026, 8, 6, 3, 4, 5),
        content="latest result",
    )
    assert second_result.id > first_result.id

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert len(activity_rows) == 1
    assert _task_rows(activity_rows) == activity_rows
    assert activity_rows[0]["session_id"] == session.session_id
    assert activity_rows[0]["completed_message_id"] == second_result.id
    assert activity_rows[0]["completed_status"] == SessionReplyWorkStatus.FAILED.value
    assert activity_rows[0]["has_unread_result"] is True


@pytest.mark.asyncio
async def test_task_sessions_are_not_truncated_over_one_hundred(api_context) -> None:
    db, _auth_state, client = api_context
    base = datetime(2026, 8, 7, 1, 2, 3)
    sessions = [
        _make_session(
            f"many-task-{index:03d}",
            "user-1",
            created_at=base + timedelta(seconds=index),
        )
        for index in range(105)
    ]
    db.add_all(sessions)
    await db.commit()
    db.add_all(
        [
            SessionReplyWorkItem(
                uid="user-1",
                session_id=session.session_id,
                profile_id=1,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id="source",
                dedupe_key=f"many-task:{index}",
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                available_at=0,
            )
            for index, session in enumerate(sessions)
        ]
    )
    await db.commit()

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert len(activity_rows) == 105
    assert all(set(row) == ACTIVITY_FIELDS for row in activity_rows)
    assert len(_task_rows(activity_rows)) == 105
    assert {row["session_id"] for row in activity_rows} == {session.session_id for session in sessions}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "auto_reply", "reply_status", "expected_running"),
    BACKGROUND_TASK_CASES,
    ids=[f"{status.value}-auto-{auto_reply}-{reply_status.value}" for status, auto_reply, reply_status, _expected in BACKGROUND_TASK_CASES],
)
async def test_background_task_states_project_running(
    api_context,
    status,
    auto_reply,
    reply_status,
    expected_running,
) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        f"background-{status.value}-{reply_status.value}",
        "user-1",
        created_at=datetime(2026, 8, 8, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    await _add_background_task(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=status,
        auto_reply=auto_reply,
        reply_status=reply_status,
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_rows = response.json()["data"]
    assert len(activity_rows) == 1
    assert set(activity_rows[0]) == ACTIVITY_FIELDS
    assert activity_rows[0]["is_owned"] is True
    assert activity_rows[0]["is_loading"] is False
    assert activity_rows[0]["is_reply_running"] is False
    data = _task_rows(activity_rows)
    if expected_running:
        assert len(data) == 1
        assert data[0]["session_id"] == session.session_id
        assert data[0]["is_running"] is True
        assert data[0]["completed_message_id"] is None
    else:
        assert data == []
        assert activity_rows[0]["is_running"] is False
