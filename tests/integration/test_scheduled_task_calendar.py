import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.session.reply_work_item as reply_work_item_module
import app.core.crud.task.scheduled as scheduled_crud_module
import app.core.security as security
import app.core.tools.scheduled_tasks as scheduled_tool_module
import app.core.utils.scheduled_time as scheduled_time_module
import app.core.utils.time as time_module
import app.models  # noqa: F401
from app.api.v1 import scheduled_tasks as scheduled_tasks_api
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_SCHEDULED_TASK_TIMEZONE_INVALID,
    ERR_SESSION_NOT_FOUND,
    ERR_UNAUTHORIZED,
)
from app.core.dispatchers.context import DispatchContext
from app.core.i18n import t
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.scheduled_time import get_next_run_times
from app.handler import register_handlers
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User
from app.providers.database import get_db

LOCAL_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 11, 2, 8, 0, tzinfo=LOCAL_TZ)
OWNER_UID = "calendar-owner"
PEER_UID = "calendar-peer"
OWNER_SESSION_ID = "calendar-owner-session"
PEER_SESSION_ID = "calendar-peer-session"
OWNER_PROFILE_ID = 1
PEER_PROFILE_ID = 2

LIST_PATH = "/api/v1/scheduled-tasks/list"
CREATE_PATH = "/api/v1/scheduled-tasks/create"
PREVIEW_PATH = "/api/v1/scheduled-tasks/preview"
UPDATE_PATH = "/api/v1/scheduled-tasks/update"


def _profile_configs() -> dict[str, Any]:
    return ProfileConfig.model_validate({}).model_dump()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    clock = {"now": FIXED_NOW}

    def now() -> datetime:
        return clock["now"]

    for module in (
        time_module,
        scheduled_time_module,
        scheduled_tasks_api,
        scheduled_crud_module,
        scheduler_module,
        scheduled_tool_module,
        reply_work_item_module,
    ):
        monkeypatch.setattr(module, "get_local_time", now)
    return clock


@pytest_asyncio.fixture
async def scheduled_task_session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        name="scheduled-task-calendar.sqlite3",
    ) as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid=OWNER_UID, username="calendar-owner"),
                    User(uid=PEER_UID, username="calendar-peer"),
                    Profile(
                        id=OWNER_PROFILE_ID,
                        uid=OWNER_UID,
                        name="Calendar owner profile",
                        is_default=True,
                        configs=_profile_configs(),
                    ),
                    Profile(
                        id=PEER_PROFILE_ID,
                        uid=PEER_UID,
                        name="Calendar peer profile",
                        is_default=True,
                        configs=_profile_configs(),
                    ),
                    ChatSession(
                        session_id=OWNER_SESSION_ID,
                        uid=OWNER_UID,
                        profile_id=OWNER_PROFILE_ID,
                    ),
                    ChatSession(
                        session_id=PEER_SESSION_ID,
                        uid=PEER_UID,
                        profile_id=PEER_PROFILE_ID,
                    ),
                ]
            )
            await db.commit()
        yield factory


@pytest_asyncio.fixture
async def db_session(
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with scheduled_task_session_factory() as db:
        yield db


@pytest_asyncio.fixture
async def api_app(db_session: AsyncSession) -> AsyncGenerator[FastAPI]:
    app = FastAPI()
    register_handlers(app)
    app.include_router(scheduled_tasks_api.router, prefix="/api/v1")
    app.state.current_user = SimpleNamespace(
        uid=OWNER_UID,
        username="calendar-owner",
        is_superuser=False,
        is_active=True,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    def override_get_current_user() -> Any:
        return app.state.current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[security.get_current_user] = override_get_current_user
    yield app


async def _task_snapshot(factory: async_sessionmaker[AsyncSession]) -> tuple[dict[str, Any], ...]:
    async with factory() as db:
        result = await db.execute(select(ScheduledTask).order_by(ScheduledTask.id))
        return tuple(task.model_dump(mode="json") for task in result.scalars().all())


def _as_local_datetime(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _standard_data(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["data"] is not None
    return payload["data"]


def _standard_error(
    response: httpx.Response,
    *,
    code: int,
    message: str | None = None,
    status_code: int = 200,
) -> None:
    assert response.status_code == status_code
    payload = response.json()
    assert payload["code"] == code
    assert payload["data"] is None
    if message is not None:
        assert payload["message"] == message


def _assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == 401
    assert payload["message"] == t(ERR_UNAUTHORIZED)
    assert payload["data"] is None


async def _persist_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    uid: str = OWNER_UID,
    session_id: str = OWNER_SESSION_ID,
    name: str = "Calendar seeded task",
    message: str = "Run the complete scheduled instruction",
    interval_seconds: int | None = None,
    cron_expression: str | None = "0 9 * * *",
    timezone_name: str | None = "Asia/Shanghai",
    status: ScheduledTaskStatus = ScheduledTaskStatus.ENABLED,
    next_run_at: datetime = FIXED_NOW + timedelta(hours=1),
    last_run_at: datetime | None = None,
    run_count: int = 0,
) -> int:
    async with factory() as db:
        task = ScheduledTask(
            name=name,
            uid=uid,
            session_id=session_id,
            message=message,
            interval_seconds=interval_seconds,
            cron_expression=cron_expression,
            timezone=timezone_name,
            status=status,
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            run_count=run_count,
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        assert task.id is not None
        return task.id


async def _api_create_task(
    client: httpx.AsyncClient,
    *,
    name: str = "Calendar API task",
    message: str = "Execute the complete calendar instruction",
    session_id: str = OWNER_SESSION_ID,
    **schedule: Any,
) -> dict[str, Any]:
    response = await client.post(
        CREATE_PATH,
        json={
            "name": name,
            "session_id": session_id,
            "message": message,
            **schedule,
        },
    )
    return _standard_data(response)


async def _api_update_task(
    client: httpx.AsyncClient,
    task_id: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(UPDATE_PATH, params={"task_id": task_id}, json=body)
    return _standard_data(response)


async def _tool_executor(
    db: AsyncSession,
    *,
    uid: str = OWNER_UID,
    session_id: str = OWNER_SESSION_ID,
    profile_id: int = OWNER_PROFILE_ID,
) -> ManageScheduledTasksExecutor:
    profile = await db.get(Profile, profile_id)
    assert profile is not None
    executor = ManageScheduledTasksExecutor(project_root=".", uid=uid)
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="calendar-integration-test",
            uid=uid,
            session_id=session_id,
            profile=profile,
            db=db,
        )
    )
    return executor


@pytest.mark.asyncio
async def test_preview_requires_auth_returns_three_future_times_without_writing(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    request = {
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        override = api_app.dependency_overrides.pop(security.get_current_user)
        try:
            unauthenticated = await client.post(PREVIEW_PATH, json=request)
        finally:
            api_app.dependency_overrides[security.get_current_user] = override
        _assert_unauthorized(unauthenticated)

        preview = _standard_data(await client.post(PREVIEW_PATH, json=request))

    expected = [
        datetime(2026, 11, 2, 9, 0, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 3, 9, 0, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 4, 9, 0, tzinfo=LOCAL_TZ),
    ]
    actual = [_as_local_datetime(item) for item in preview["next_run_times"]]
    assert actual == expected
    assert len(actual) == 3
    assert all(run > FIXED_NOW for run in actual)
    assert await _task_snapshot(scheduled_task_session_factory) == before


CREATE_CRON_CASES = (
    pytest.param(
        "0 9 * * *",
        (
            datetime(2026, 11, 2, 9, 0, tzinfo=LOCAL_TZ),
            datetime(2026, 11, 3, 9, 0, tzinfo=LOCAL_TZ),
        ),
        id="daily",
    ),
    pytest.param(
        "30 14 * * 1",
        (
            datetime(2026, 11, 2, 14, 30, tzinfo=LOCAL_TZ),
            datetime(2026, 11, 9, 14, 30, tzinfo=LOCAL_TZ),
        ),
        id="weekly",
    ),
    pytest.param(
        "0 20 L * *",
        (
            datetime(2026, 11, 30, 20, 0, tzinfo=LOCAL_TZ),
            datetime(2026, 12, 31, 20, 0, tzinfo=LOCAL_TZ),
        ),
        id="month-end",
    ),
    pytest.param(
        "0 20 31 * *",
        (
            datetime(2026, 12, 31, 20, 0, tzinfo=LOCAL_TZ),
            datetime(2027, 1, 31, 20, 0, tzinfo=LOCAL_TZ),
        ),
        id="day-31-skips-november",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("cron_expression,expected_runs", CREATE_CRON_CASES)
async def test_create_persists_daily_weekly_and_calendar_month_rules(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    cron_expression: str,
    expected_runs: tuple[datetime, datetime],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        data = await _api_create_task(
            client,
            cron_expression=cron_expression,
            timezone="Asia/Shanghai",
        )

    assert data["interval_seconds"] is None
    assert data["cron_expression"] == cron_expression.lower()
    assert data["timezone"] == "Asia/Shanghai"
    assert data["run_once"] is False
    assert _as_local_datetime(data["next_run_at"]) == expected_runs[0]

    async with scheduled_task_session_factory() as db:
        task = await db.get(ScheduledTask, data["id"])
        assert task is not None
        assert task.interval_seconds is None
        assert task.cron_expression == cron_expression.lower()
        assert task.timezone == "Asia/Shanghai"
        assert _as_local_datetime(task.next_run_at) == expected_runs[0]

    assert get_next_run_times(
        cron_expression=cron_expression,
        timezone="Asia/Shanghai",
        after=FIXED_NOW,
        count=2,
    ) == list(expected_runs)


ANNUAL_CRON_CASES = (
    pytest.param(
        "30 8 1 1 *",
        (
            datetime(2027, 1, 1, 8, 30, tzinfo=LOCAL_TZ),
            datetime(2028, 1, 1, 8, 30, tzinfo=LOCAL_TZ),
            datetime(2029, 1, 1, 8, 30, tzinfo=LOCAL_TZ),
        ),
        id="annual-january-first",
    ),
    pytest.param(
        "0 9 29 2 *",
        (
            datetime(2028, 2, 29, 9, 0, tzinfo=LOCAL_TZ),
            datetime(2032, 2, 29, 9, 0, tzinfo=LOCAL_TZ),
            datetime(2036, 2, 29, 9, 0, tzinfo=LOCAL_TZ),
        ),
        id="annual-leap-day",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("cron_expression,expected_runs", ANNUAL_CRON_CASES)
async def test_annual_cron_preview_create_update_and_persistence(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    cron_expression: str,
    expected_runs: tuple[datetime, datetime, datetime],
) -> None:
    timezone_name = "Asia/Shanghai"
    before_preview = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        preview = _standard_data(
            await client.post(
                PREVIEW_PATH,
                json={"cron_expression": cron_expression, "timezone": timezone_name},
            )
        )

        preview_runs = [_as_local_datetime(item) for item in preview["next_run_times"]]
        assert preview_runs == list(expected_runs)
        assert await _task_snapshot(scheduled_task_session_factory) == before_preview

        created = await _api_create_task(
            client,
            cron_expression=cron_expression,
            timezone=timezone_name,
        )
        assert _as_local_datetime(created["next_run_at"]) == preview_runs[0]
        assert created["interval_seconds"] is None
        assert created["cron_expression"] == cron_expression
        assert created["timezone"] == timezone_name
        assert created["run_once"] is False

        interval = await _api_update_task(client, created["id"], {"interval_seconds": 60})
        assert interval["interval_seconds"] == 60
        assert interval["cron_expression"] is None
        assert interval["timezone"] is None
        assert interval["run_once"] is False
        assert _as_local_datetime(interval["next_run_at"]) == FIXED_NOW + timedelta(seconds=60)

        annual = await _api_update_task(
            client,
            created["id"],
            {"cron_expression": cron_expression, "timezone": timezone_name},
        )

    assert _as_local_datetime(annual["next_run_at"]) == preview_runs[0]
    assert annual["interval_seconds"] is None
    assert annual["cron_expression"] == cron_expression
    assert annual["timezone"] == timezone_name
    assert annual["run_once"] is False

    async with scheduled_task_session_factory() as db:
        task = await db.get(ScheduledTask, created["id"])
        assert task is not None
        assert task.interval_seconds is None
        assert task.cron_expression == cron_expression
        assert task.timezone == timezone_name
        assert task.run_once is False
        assert _as_local_datetime(task.next_run_at) == _as_local_datetime(annual["next_run_at"])


@pytest.mark.asyncio
async def test_update_switches_interval_and_cron_modes_and_respects_run_once(api_app: FastAPI) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        created = await _api_create_task(client, interval_seconds=60)
        task_id = created["id"]

        one_time = await _api_update_task(client, task_id, {"run_once": True})
        assert one_time["interval_seconds"] == 60
        assert one_time["cron_expression"] is None
        assert one_time["timezone"] is None
        assert one_time["run_once"] is True
        assert _as_local_datetime(one_time["next_run_at"]) == FIXED_NOW + timedelta(seconds=60)

        cron = await _api_update_task(
            client,
            task_id,
            {
                "cron_expression": "0 9 * * *",
                "timezone": "Asia/Shanghai",
            },
        )
        assert cron["interval_seconds"] is None
        assert cron["cron_expression"] == "0 9 * * *"
        assert cron["timezone"] == "Asia/Shanghai"
        assert cron["run_once"] is False
        assert _as_local_datetime(cron["next_run_at"]) == datetime(2026, 11, 2, 9, tzinfo=LOCAL_TZ)

        interval = await _api_update_task(client, task_id, {"interval_seconds": 120})

    assert interval["interval_seconds"] == 120
    assert interval["cron_expression"] is None
    assert interval["timezone"] is None
    assert interval["run_once"] is False
    assert _as_local_datetime(interval["next_run_at"]) == FIXED_NOW + timedelta(seconds=120)


@pytest.mark.asyncio
async def test_update_message_only_keeps_calendar_next_run_at(api_app: FastAPI) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        created = await _api_create_task(
            client,
            message="Original complete instruction",
            cron_expression="0 9 * * *",
            timezone="Asia/Shanghai",
        )
        original_next = _as_local_datetime(created["next_run_at"])
        updated = await _api_update_task(
            client,
            created["id"],
            {"message": "Updated complete instruction with all details"},
        )

    assert updated["message"] == "Updated complete instruction with all details"
    assert updated["cron_expression"] == "0 9 * * *"
    assert updated["timezone"] == "Asia/Shanghai"
    assert _as_local_datetime(updated["next_run_at"]) == original_next


@pytest.mark.asyncio
async def test_disabling_and_reenabling_cron_recomputes_from_current_calendar_time(
    api_app: FastAPI,
    fixed_clock: dict[str, datetime],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        created = await _api_create_task(
            client,
            cron_expression="0 9 * * *",
            timezone="Asia/Shanghai",
        )
        original_next = _as_local_datetime(created["next_run_at"])
        disabled = await _api_update_task(client, created["id"], {"status": ScheduledTaskStatus.DISABLED.value})
        assert disabled["status"] == ScheduledTaskStatus.DISABLED.value
        assert _as_local_datetime(disabled["next_run_at"]) == original_next

        fixed_clock["now"] = datetime(2026, 11, 3, 10, 15, tzinfo=LOCAL_TZ)
        reenabled = await _api_update_task(client, created["id"], {"status": ScheduledTaskStatus.ENABLED.value})

    assert reenabled["status"] == ScheduledTaskStatus.ENABLED.value
    assert _as_local_datetime(reenabled["next_run_at"]) == datetime(2026, 11, 4, 9, tzinfo=LOCAL_TZ)


INVALID_CALENDAR_CASES = (
    pytest.param(
        {"cron_expression": "0 9 * *", "timezone": "Asia/Shanghai"},
        id="invalid-cron-fields",
    ),
    pytest.param(
        {"cron_expression": "0 9 * * *", "timezone": "Mars/Olympus"},
        id="invalid-timezone",
    ),
    pytest.param(
        {
            "interval_seconds": 60,
            "cron_expression": "0 9 * * *",
            "timezone": "Asia/Shanghai",
        },
        id="interval-cron-conflict",
    ),
    pytest.param(
        {"cron_expression": "0 0 31 4 *", "timezone": "UTC"},
        id="impossible-calendar-date",
    ),
    pytest.param(
        {"cron_expression": "0 9 * * *"},
        id="missing-timezone",
    ),
    pytest.param(
        {"cron_expression": "0 9 * * *", "timezone": "Asia/Shanghai", "run_once": True},
        id="cron-run-once-conflict",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", INVALID_CALENDAR_CASES)
async def test_create_invalid_calendar_schedule_does_not_write_tasks(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    schedule: dict[str, Any],
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            CREATE_PATH,
            json={
                "name": "Rejected calendar task",
                "session_id": OWNER_SESSION_ID,
                "message": "This must not be persisted",
                **schedule,
            },
        )

    assert response.status_code in {400, 422}
    assert response.json()["code"] == response.status_code
    assert await _task_snapshot(scheduled_task_session_factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", INVALID_CALENDAR_CASES)
async def test_update_invalid_calendar_schedule_does_not_change_tasks(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    schedule: dict[str, Any],
) -> None:
    task_id = await _persist_task(
        scheduled_task_session_factory,
        interval_seconds=60,
        cron_expression=None,
        timezone_name=None,
        next_run_at=FIXED_NOW + timedelta(seconds=60),
    )
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(UPDATE_PATH, params={"task_id": task_id}, json=schedule)

    assert response.status_code in {400, 422}
    assert response.json()["code"] == response.status_code
    assert await _task_snapshot(scheduled_task_session_factory) == before


@pytest.mark.asyncio
async def test_calendar_api_cannot_expand_profile_permissions(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    peer_task_id = await _persist_task(
        scheduled_task_session_factory,
        uid=PEER_UID,
        session_id=PEER_SESSION_ID,
        name="Peer calendar task",
        message="Peer-owned instruction",
        next_run_at=FIXED_NOW + timedelta(hours=1),
    )
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        create_response = await client.post(
            CREATE_PATH,
            json={
                "name": "Forged peer calendar task",
                "session_id": PEER_SESSION_ID,
                "message": "Must remain uncreated",
                "cron_expression": "0 10 * * *",
                "timezone": "Asia/Shanghai",
                "uid": PEER_UID,
                "profile_id": PEER_PROFILE_ID,
                "is_superuser": True,
            },
        )
        _standard_error(create_response, code=404, message=t(ERR_SESSION_NOT_FOUND), status_code=404)

        update_response = await client.post(
            UPDATE_PATH,
            params={"task_id": peer_task_id, "uid": PEER_UID, "is_admin": "true"},
            json={
                "message": "Forged peer update",
                "cron_expression": "0 10 * * *",
                "timezone": "Asia/Shanghai",
                "profile_id": PEER_PROFILE_ID,
                "is_superuser": True,
            },
        )
        _standard_error(update_response, code=404, message=t(ERR_BACKGROUND_TASK_NOT_FOUND))

    assert await _task_snapshot(scheduled_task_session_factory) == before


@pytest.mark.asyncio
async def test_scheduler_dispatches_due_calendar_task_once_with_complete_agent_work(
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    late_now = datetime(2026, 11, 2, 10, 15, tzinfo=LOCAL_TZ)
    fixed_clock["now"] = late_now
    instruction = "At 09:00 review /srv/reports/deploy.json and list every failed deployment with its exact ID."
    task_id = await _persist_task(
        scheduled_task_session_factory,
        message=instruction,
        cron_expression="0 9 * * *",
        timezone_name="Asia/Shanghai",
        next_run_at=datetime(2026, 11, 2, 9, tzinfo=LOCAL_TZ),
        last_run_at=datetime(2026, 11, 1, 9, tzinfo=LOCAL_TZ),
        run_count=4,
    )
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", scheduled_task_session_factory)
    scheduler = scheduler_module.ScheduledTaskScheduler()

    await scheduler._dispatch_one(task_id)
    await scheduler._dispatch_one(task_id)

    async with scheduled_task_session_factory() as db:
        persisted_task = await db.get(ScheduledTask, task_id)
        trigger_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == OWNER_SESSION_ID,
                        Message.type == MessageType.SCHEDULED_TASK_TRIGGER,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        work_items = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem)
                    .where(
                        SessionReplyWorkItem.session_id == OWNER_SESSION_ID,
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    )
                    .order_by(SessionReplyWorkItem.id)
                )
            )
            .scalars()
            .all()
        )

    assert persisted_task is not None
    assert persisted_task.status == ScheduledTaskStatus.ENABLED
    assert persisted_task.cron_expression == "0 9 * * *"
    assert persisted_task.timezone == "Asia/Shanghai"
    assert persisted_task.run_count == 5
    assert _as_local_datetime(persisted_task.last_run_at) == late_now
    assert _as_local_datetime(persisted_task.next_run_at) == datetime(2026, 11, 3, 9, tzinfo=LOCAL_TZ)
    assert _as_local_datetime(persisted_task.next_run_at) != late_now + timedelta(days=1)

    expected_future_runs = get_next_run_times(
        cron_expression="0 9 * * *",
        timezone="Asia/Shanghai",
        after=late_now,
        count=3,
    )
    assert expected_future_runs == [
        datetime(2026, 11, 3, 9, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 4, 9, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 5, 9, tzinfo=LOCAL_TZ),
    ]
    assert _as_local_datetime(persisted_task.next_run_at) == expected_future_runs[0]

    assert len(trigger_messages) == 1
    trigger_message = trigger_messages[0]
    assert persisted_task.last_message_id == trigger_message.id
    assert trigger_message.content == instruction
    assert trigger_message.role == MessageRole.USER
    assert trigger_message.profile_id == OWNER_PROFILE_ID
    assert trigger_message.is_processed is True
    assert trigger_message.work_id is not None

    assert len(work_items) == 1
    queued_work = work_items[0]
    assert queued_work.id == trigger_message.work_id
    assert queued_work.uid == OWNER_UID
    assert queued_work.profile_id == OWNER_PROFILE_ID
    assert queued_work.work_type == SessionReplyWorkType.AGENT_WORK
    assert queued_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert queued_work.source_id == str(trigger_message.id)
    assert queued_work.execution_state["frozen_context_messages"] == []
    queued_spec = AgentWorkSpec.model_validate(queued_work.execution_state["agent_work"])
    assert queued_spec.instruction == instruction
    assert queued_spec.execution_scope == END_SESSION_SCOPE_TASK
    assert queued_spec.report_target == AgentWorkReportTarget.SESSION
    assert queued_spec.instruction_message_id == trigger_message.id


@pytest.mark.asyncio
async def test_tool_creates_cron_persists_saved_first_next_run_and_rejects_missing_timezone(
    db_session: AsyncSession,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = await _tool_executor(db_session)
    instruction = "At 09:00 inspect /srv/reports/deploy.json and report failed deployment IDs."
    created = json.loads(
        await executor.execute(
            "create",
            name="Tool calendar task",
            message=instruction,
            cron_expression="0 9 * * *",
            timezone="Asia/Shanghai",
        )
    )
    assert created["status"] == "success"
    task_id = created["task"]["id"]

    async with scheduled_task_session_factory() as reader:
        persisted = await reader.get(ScheduledTask, task_id)
        assert persisted is not None
        saved_next_run = _as_local_datetime(persisted.next_run_at)

    returned_next_runs = [_as_local_datetime(value) for value in created["next_run_times"]]
    assert len(returned_next_runs) == 3
    assert returned_next_runs[0] == saved_next_run
    assert returned_next_runs == [
        datetime(2026, 11, 2, 9, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 3, 9, tzinfo=LOCAL_TZ),
        datetime(2026, 11, 4, 9, tzinfo=LOCAL_TZ),
    ]
    assert persisted.cron_expression == "0 9 * * *"
    assert persisted.timezone == "Asia/Shanghai"
    assert persisted.message == instruction

    before = await _task_snapshot(scheduled_task_session_factory)
    missing_timezone = json.loads(
        await executor.execute(
            "create",
            name="Rejected tool calendar task",
            message="This must not be stored",
            cron_expression="0 10 * * *",
        )
    )
    assert missing_timezone["status"] == "failed"
    assert missing_timezone["error"] == t(ERR_SCHEDULED_TASK_TIMEZONE_INVALID)
    assert await _task_snapshot(scheduled_task_session_factory) == before
