from app.schemas.response import StandardResponse
from app.models.message import Message
from app.models.user import User
from sqlalchemy import select, func, desc
import uuid
from fastapi import APIRouter, Depends
from app.core.security import get_current_user
from app.core.dispatcher import ChatDispatcher
from app.schemas.message import ChatCompletionRequest
from app.providers.database import AsyncSession, get_db
from app.transformers.openai import OpenAITransformer

router = APIRouter(
    prefix="/chat", tags=["Chat"], dependencies=[Depends(get_current_user)]
)


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 异步等待调度器返回真实推理结果
    session_id = request.session_id or str(uuid.uuid4())
    llm_response = await ChatDispatcher.dispatch(
        db, request.message, uid=current_user["uid"], session_id=session_id
    )
    # 格式化输出
    return OpenAITransformer.from_standard(llm_response)


@router.get("/sessions/list")
async def get_user_sessions(
    db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)
):
    # 查询会话列表：普通用户仅限自己，超级管理员可查看全部
    uid = current_user["uid"]
    is_admin = current_user.get("is_superuser", False)

    stmt = select(
        Message.session_id,
        func.max(Message.created_at).label("last_active"),
        Message.uid,
        User.username,
    ).join(User, Message.uid == User.uid)

    if not is_admin:
        stmt = stmt.where(Message.uid == uid)

    stmt = stmt.group_by(Message.session_id, Message.uid, User.username).order_by(
        desc("last_active")
    )

    result = await db.execute(stmt)
    sessions = result.all()

    data = [
        {
            "session_id": row[0],
            "last_active": row[1].strftime("%Y-%m-%d %H:%M:%S") if row[1] else None,
            "username": row[3],
        }
        for row in sessions
    ]

    return StandardResponse.success(data=data, message="会话列表获取成功")


from sqlalchemy import delete


@router.post("/sessions/delete")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # 删除会话：普通用户仅限自己，超级管理员可跨用户删除
    uid = current_user["uid"]
    is_admin = current_user.get("is_superuser", False)

    stmt = delete(Message).where(Message.session_id == session_id)
    if not is_admin:
        stmt = stmt.where(Message.uid == uid)
    result = await db.execute(stmt)
    await db.commit()

    if result.rowcount == 0:
        return StandardResponse.success(message="会话未找到或已删除")

    return StandardResponse.success(
        message=f"已成功清理会话 {session_id} 的全部历史记录"
    )
