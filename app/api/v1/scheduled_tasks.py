from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_BACKGROUND_TASK_NOT_FOUND, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, ERR_SESSION_NOT_FOUND, MSG_GENERIC_SUCCESS
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.profile.selection import resolve_profile_for_session
from app.core.security import get_current_user
from app.core.utils.scheduled_time import get_next_run_times
from app.core.utils.time import get_local_time
from app.models.scheduled_task import ScheduledTaskResponse, ScheduledTaskStatus
from app.providers.database import get_db
from app.schemas.response import PageData, StandardResponse
from app.schemas.scheduled_task import ScheduledTaskCreateRequest, ScheduledTaskSchedule, ScheduledTaskUpdateRequest

router = APIRouter(prefix="/scheduled-tasks", tags=["ScheduledTasks"], dependencies=[Depends(get_current_user)])


async def _validate_user_session(db: AsyncSession, session_id: str, uid: str) -> None:
    session = await session_crud.get_by_session_id(db, session_id)
    if not session or session.uid != uid:
        raise ResourceNotFoundException(message=ERR_SESSION_NOT_FOUND)


async def _get_session_profile_id(db: AsyncSession, session_id: str, uid: str) -> int | None:
    session = await session_crud.get_by_session_id(db, session_id)
    if not session or session.uid != uid:
        return None
    profile = await resolve_profile_for_session(db, uid=uid, session_id=session_id)
    if profile is None or profile.id is None:
        return None
    locked_profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile.id,
        uid=uid,
    )
    return locked_profile.id if locked_profile is not None else None


@router.get("/list")
async def list_scheduled_tasks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: ScheduledTaskStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    skip = (page - 1) * size
    is_admin = current_user.is_superuser
    tasks = await scheduled_task_crud.list_tasks(
        db,
        skip=skip,
        limit=size,
        status=status,
        uid=current_user.uid,
        is_admin=is_admin,
    )
    total = await scheduled_task_crud.count_tasks(
        db,
        status=status,
        uid=current_user.uid,
        is_admin=is_admin,
    )
    users = await user_crud.get_multi_by_uids(db, list({task.uid for task in tasks}))
    usernames = {user.uid: user.username for user in users}
    items = []
    for task in tasks:
        response = ScheduledTaskResponse.model_validate(task)
        response.username = usernames.get(task.uid)
        items.append(response)
    data = PageData(items=items, total=total, page=page, size=size)
    return StandardResponse.success(data=data, message=MSG_GENERIC_SUCCESS)


@router.post("/create")
async def create_scheduled_task(
    request: ScheduledTaskCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    session = await session_crud.get_by_session_id(db, request.session_id)
    if not session or (not current_user.is_superuser and session.uid != current_user.uid):
        raise ResourceNotFoundException(message=ERR_SESSION_NOT_FOUND)

    profile_id = await _get_session_profile_id(db, request.session_id, session.uid)
    if profile_id is None:
        return StandardResponse.error(code=400, message=ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)

    task = await scheduled_task_crud.create_scheduled_task(
        db,
        name=request.name,
        uid=session.uid,
        session_id=request.session_id,
        message=request.message,
        interval_seconds=request.interval_seconds,
        cron_expression=request.cron_expression,
        timezone=request.timezone,
        run_once=request.run_once,
    )
    return StandardResponse.success(data=ScheduledTaskResponse.model_validate(task), message=MSG_GENERIC_SUCCESS)


@router.post("/preview")
async def preview_scheduled_task(request: ScheduledTaskSchedule):
    return StandardResponse.success(
        data={
            "next_run_times": get_next_run_times(
                **request.model_dump(),
                after=get_local_time(),
                count=3,
            )
        },
        message=MSG_GENERIC_SUCCESS,
    )


@router.post("/update")
async def update_scheduled_task(
    task_id: int,
    request: ScheduledTaskUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    task = await scheduled_task_crud.get(db, task_id)
    if not task or (not current_user.is_superuser and task.uid != current_user.uid):
        return StandardResponse.error(code=404, message=ERR_BACKGROUND_TASK_NOT_FOUND)

    obj_in = request.model_dump(exclude_unset=True, exclude_none=True)
    session_id = obj_in.get("session_id", task.session_id)
    await _validate_user_session(db, session_id, task.uid)

    interval_seconds_provided = "interval_seconds" in obj_in
    schedule_values = {
        "interval_seconds": task.interval_seconds,
        "cron_expression": task.cron_expression,
        "timezone": task.timezone,
        "run_once": task.run_once,
    }
    for field in ("interval_seconds", "cron_expression", "timezone", "run_once"):
        if field in obj_in:
            schedule_values[field] = obj_in[field]

    if "cron_expression" in obj_in and "interval_seconds" not in obj_in:
        schedule_values["interval_seconds"] = None
        if "run_once" not in obj_in:
            schedule_values["run_once"] = False
    elif "interval_seconds" in obj_in and "cron_expression" not in obj_in:
        schedule_values["cron_expression"] = None
        if "timezone" not in obj_in:
            schedule_values["timezone"] = None

    schedule = ScheduledTaskSchedule(**schedule_values)
    obj_in["interval_seconds"] = schedule.interval_seconds
    obj_in["cron_expression"] = schedule.cron_expression
    obj_in["timezone"] = schedule.timezone
    obj_in["run_once"] = schedule.run_once

    run_once_changed = schedule.run_once != task.run_once
    cron_changed = schedule.cron_expression != task.cron_expression
    timezone_changed = schedule.timezone != task.timezone
    status_enabled = obj_in.get("status") == ScheduledTaskStatus.ENABLED
    task_was_disabled = task.status != ScheduledTaskStatus.ENABLED
    one_time_reenabled = schedule.run_once and status_enabled and task_was_disabled
    cron_reenabled = schedule.cron_expression is not None and status_enabled and task_was_disabled

    should_reschedule = interval_seconds_provided
    if run_once_changed:
        should_reschedule = True
    if cron_changed or timezone_changed:
        should_reschedule = True
    if one_time_reenabled or cron_reenabled:
        should_reschedule = True
    if should_reschedule:
        obj_in["next_run_at"] = get_next_run_times(
            interval_seconds=schedule.interval_seconds,
            cron_expression=schedule.cron_expression,
            timezone=schedule.timezone,
            run_once=schedule.run_once,
            after=get_local_time(),
            count=1,
        )[0]
    if obj_in.get("status") == ScheduledTaskStatus.ENABLED or "session_id" in obj_in:
        profile_id = await _get_session_profile_id(db, session_id, task.uid)
        if profile_id is None:
            return StandardResponse.error(code=400, message=ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)
    updated_task = await scheduled_task_crud.update_scheduled_task(db, scheduled_task=task, obj_in=obj_in)
    return StandardResponse.success(data=ScheduledTaskResponse.model_validate(updated_task), message=MSG_GENERIC_SUCCESS)


@router.post("/delete")
async def delete_scheduled_task(task_id: int, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)):
    task = await scheduled_task_crud.get(db, task_id)
    if not task or (not current_user.is_superuser and task.uid != current_user.uid):
        return StandardResponse.error(code=404, message=ERR_BACKGROUND_TASK_NOT_FOUND)
    await scheduled_task_crud.delete_task(db, scheduled_task=task)
    return StandardResponse.success(message=MSG_GENERIC_SUCCESS)
