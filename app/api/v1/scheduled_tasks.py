from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_BACKGROUND_TASK_NOT_FOUND, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, ERR_SESSION_NOT_FOUND, MSG_GENERIC_SUCCESS
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.security import get_current_user
from app.core.utils.time import get_local_time
from app.models.scheduled_task import ScheduledTaskResponse, ScheduledTaskStatus
from app.providers.database import get_db
from app.schemas.response import PageData, StandardResponse
from app.schemas.scheduled_task import ScheduledTaskCreateRequest, ScheduledTaskUpdateRequest

router = APIRouter(prefix="/scheduled-tasks", tags=["ScheduledTasks"], dependencies=[Depends(get_current_user)])


async def _validate_user_session(db: AsyncSession, session_id: str, uid: str) -> None:
    session = await session_crud.get_by_session_id(db, session_id)
    if not session or session.uid != uid:
        raise ResourceNotFoundException(message=ERR_SESSION_NOT_FOUND)


async def _get_session_profile_id(db: AsyncSession, session_id: str, uid: str) -> int | None:
    session = await session_crud.get_by_session_id(db, session_id)
    if not session or session.uid != uid:
        return None
    if session.profile_id is None:
        return None
    profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=session.profile_id,
        uid=uid,
    )
    return profile.id if profile is not None else None


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
        profile_id=profile_id,
        message=request.message,
        interval_seconds=request.interval_seconds,
    )
    return StandardResponse.success(data=ScheduledTaskResponse.model_validate(task), message=MSG_GENERIC_SUCCESS)


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

    if "interval_seconds" in obj_in:
        obj_in["next_run_at"] = get_local_time() + timedelta(seconds=obj_in["interval_seconds"])
    if obj_in.get("status") == ScheduledTaskStatus.ENABLED or "session_id" in obj_in:
        profile_id = await _get_session_profile_id(db, session_id, task.uid)
        if profile_id is None:
            return StandardResponse.error(code=400, message=ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)
        obj_in["profile_id"] = profile_id
    updated_task = await scheduled_task_crud.update_scheduled_task(db, scheduled_task=task, obj_in=obj_in)
    return StandardResponse.success(data=ScheduledTaskResponse.model_validate(updated_task), message=MSG_GENERIC_SUCCESS)


@router.post("/delete")
async def delete_scheduled_task(task_id: int, db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)):
    task = await scheduled_task_crud.get(db, task_id)
    if not task or (not current_user.is_superuser and task.uid != current_user.uid):
        return StandardResponse.error(code=404, message=ERR_BACKGROUND_TASK_NOT_FOUND)
    await scheduled_task_crud.delete_task(db, scheduled_task=task)
    return StandardResponse.success(message=MSG_GENERIC_SUCCESS)
