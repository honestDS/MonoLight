import asyncio

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.dispatcher import ChatDispatcher
from app.core.log import (
    get_logger,
)
from app.core.security import get_current_user
from app.models.message import (
    ChatCompletionRequest,
    MessageResponse,
    MessageRole,
    MessageType,
)
from app.providers.database import get_db
from app.schemas.response import StandardResponse

logger = get_logger(__name__)


router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
    dependencies=[Depends(get_current_user)]
)


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)

    # 使用适配器处理对话请求
    return await web_chat_adapter.chat(
        db=db,
        message=request.message,
        uid=uid,
        session_id=request.session_id
    )


@router.get("/sessions/list")
async def get_user_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)
    sessions = await message_crud.get_user_sessions(
        db,
        uid=uid,
        is_admin=is_admin
    )

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
        db,
        session_id=session_id,
        uid=uid,
        is_admin=is_admin
    )

    if row_count == 0:
        return StandardResponse.success(message="会话未找到或已删除")

    return StandardResponse.success(
        message=f"已成功清理会话 {session_id} 的全部历史记录"
    )


@router.get("/sessions/history")
async def get_session_history(
    session_id: str,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    uid = getattr(current_user, "uid", None)
    offset = (page - 1) * size
    messages = await message_crud.get_history_paged(
        db,
        session_id=session_id,
        uid=uid,
        limit=size,
        offset=offset
    )

    # 倒序取出，正序返回
    messages.reverse()

    data = [MessageResponse.model_validate(m) for m in messages]
    return StandardResponse.success(data=data, message="会话历史记录获取成功")


@router.websocket("/ws")
async def chat_websocket(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    WebSocket 对话接口
    认证方式与 HTTP 接口一致（通常通过 Query Token 或 Header）
    """
    await websocket.accept()
    uid = getattr(current_user, "uid", None)

    # 用于追踪当前是否有正在运行的调度任务
    active_task = None

    async def run_chat(message_text, session_id):
        nonlocal active_task
        try:
            async for response in ws_chat_adapter.chat(
                db=db,
                message=message_text,
                uid=uid,
                session_id=session_id
            ):
                await websocket.send_json(response)
        except RuntimeError as e:
            # 拦截断开连接后的发送错误
            if "websocket.send" in str(e) and "websocket.close" in str(e):
                logger.bind(uid=uid, session_id=session_id).info("用户已断开连接,调度器终止")
            else:
                logger.bind(uid=uid, session_id=session_id).error(f"WS task runtime error: {e}")
        except asyncio.CancelledError:
            logger.bind(uid=uid, session_id=session_id).info("用户已断开连接,调度器终止")
            raise
        except Exception:
            logger.bind(uid=uid, session_id=session_id).exception("WS task error")
            try:
                await websocket.send_json({"type": "error", "message": "Internal server error"})
            except:
                pass
        finally:
            active_task = None

    try:
        while True:
            # 接收 JSON 消息
            data = await websocket.receive_json()
            message = data.get("message")
            session_id = data.get("session_id")

            if not message:
                await websocket.send_json({"error": "Message is required"})
                continue

            # 如果当前已有任务在运行，新消息仅需保存到数据库
            if active_task and not active_task.done():
                profile = await profile_crud.get_active(db)
                await ChatDispatcher._save_message(
                    db,
                    session_id,
                    uid,
                    MessageRole.USER,
                    MessageType.TEXT,
                    message,
                    profile.id if profile else -1
                )
                logger.bind(uid=uid, session_id=session_id).info(
                    f"Existing active task found for session {session_id}, message saved to DB for dynamic append."
                )
            else:
                # 否则启动新的调度任务
                active_task = asyncio.create_task(run_chat(message, session_id))

    except WebSocketDisconnect:
        # 连接正常关闭
        if active_task and not active_task.done():
            active_task.cancel()
    except Exception:
        # 异常处理
        logger.bind(uid=uid).exception("chat/ws error")
        try:
            await websocket.send_json({"error": "Internal server error"})
        except:
            pass
        if active_task and not active_task.done():
            active_task.cancel()
        await websocket.close()
    finally:
        # 确保任务被取消
        if active_task and not active_task.done():
            active_task.cancel()
