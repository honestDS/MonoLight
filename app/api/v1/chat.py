import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.dispatcher import ChatDispatcher
from app.core.security import get_current_user
from app.providers.database import get_db
from app.models.message import ChatCompletionRequest
from app.schemas.response import StandardResponse
from app.core.crud.message import message_crud

router = APIRouter(
    prefix="/chat", tags=["Chat"], dependencies=[Depends(get_current_user)]
)


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    session_id = request.session_id or str(uuid.uuid4())
    llm_response = await ChatDispatcher.dispatch(
        db,
        request.message,
        uid=getattr(current_user, "uid", None),
        session_id=session_id,
    )
    return llm_response


@router.get("/sessions/list")
async def get_user_sessions(
    db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)
):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)
    sessions = await message_crud.get_user_sessions(db, uid=uid, is_admin=is_admin)

    data = [
        {
            "session_id": row[0],
            "last_active": row[1].strftime("%Y-%m-%d %H:%M:%S") if row[1] else None,
            "username": row[3],
        }
        for row in sessions
    ]
    return StandardResponse.success(data=data, message="会话列表获取成功")


@router.post("/sessions/delete")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)
    row_count = await message_crud.remove_session(
        db, session_id=session_id, uid=uid, is_admin=is_admin
    )

    if row_count == 0:
        return StandardResponse.success(message="会话未找到或已删除")

    return StandardResponse.success(
        message=f"已成功清理会话 {session_id} 的全部历史记录"
    )
