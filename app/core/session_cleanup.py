from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.crud.message_platform_outbox import message_platform_outbox_crud
from app.core.crud.scheduled_task import scheduled_task_crud
from app.core.crud.session import session_crud
from app.core.crud.session_event import session_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud


async def delete_session_data(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str | None,
    is_admin: bool,
) -> bool:
    session = await session_crud.get_by_session_id(db, session_id)
    if session is None or (not is_admin and session.uid != uid):
        return False

    await session_reply_work_item_crud.delete_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await session_event_crud.delete_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await message_platform_outbox_crud.delete_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await background_task_crud.cleanup_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await scheduled_task_crud.delete_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await message_crud.remove_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    await session_crud.delete_by_session(
        db,
        session_id=session_id,
        is_admin=True,
        commit=False,
    )
    return True
