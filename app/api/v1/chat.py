import asyncio
import time
import uuid

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.crud.provider import provider_crud
from app.core.log import (
    get_logger,
)
from app.core.security import get_current_user
from app.core.utils.session import generate_session_title
from app.models.message import (
    ChatCompletionRequest,
    MessageResponse,
    MessageRole,
)
from app.models.profile import ProfileConfig
from app.models.provider import ModelUsage
from app.providers.database import AsyncSessionLocal, get_db
from app.schemas.response import (
    LLMChoice,
    LLMChoiceMessage,
    LLMResponse,
    StandardResponse,
)

logger = get_logger(__name__)


router = APIRouter(prefix="/chat", tags=["Chat"], dependencies=[Depends(get_current_user)])


@router.post("/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)

    # 如果 session_id 为空，直接生成并返回，由前端发起二次请求
    if not request.session_id:
        new_session_id = str(uuid.uuid4())
        return LLMResponse(
            choices=[
                LLMChoice(
                    message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=new_session_id),
                    finish_reason="new_session",
                    created_at=time.time(),
                )
            ],
            history=[],
        ).model_dump()

    # 使用适配器处理对话请求
    return await web_chat_adapter.chat(
        db=db,
        message=request.message,
        uid=uid,
        session_id=request.session_id,
        attachments=request.attachments,
    )


@router.get("/sessions/list")
async def get_user_sessions(db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)
    sessions = await message_crud.get_user_sessions(db, uid=uid, is_admin=is_admin)

    data = [
        {
            "session_id": row.session_id,
            "last_active": row.last_active.strftime("%Y-%m-%d %H:%M:%S") if row.last_active else None,
            "username": row.username,
            "title": row.title,
            "enable_markdown": row.enable_markdown,
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
    row_count = await message_crud.remove_session(db, session_id=session_id, uid=uid, is_admin=is_admin)

    if row_count == 0:
        return StandardResponse.success(message="会话未找到或已删除")

    return StandardResponse.success(message=f"已成功清理会话 {session_id} 的全部历史记录")


class SessionSettingRequest(BaseModel):
    session_id: str
    enable_markdown: bool


@router.post("/sessions/setting")
async def update_session_setting(
    request: SessionSettingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.core.crud.session import session_crud

    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)

    session = await session_crud.get_by_session_id(db, request.session_id)
    if not session:
        return StandardResponse.error(message="会话未找到")

    if not is_admin and session.uid != uid:
        return StandardResponse.error(message="无权操作此会话")

    session.enable_markdown = request.enable_markdown
    await db.commit()

    return StandardResponse.success(message="会话设置已更新")


class SessionTitleGenerateRequest(BaseModel):
    session_id: str
    first_message: str


@router.post("/sessions/generate-title")
async def generate_title(
    request: SessionTitleGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    profile = await profile_crud.get_active(db)
    if not profile:
        return StandardResponse.error(message="未配置有效的模型提供商")

    cfg = ProfileConfig.model_validate(profile.configs)
    provider_id = cfg.provider.provider_id
    if not provider_id or provider_id <= 0:
        return StandardResponse.error(message="未配置有效的模型提供商")

    provider = await provider_crud.get(db, provider_id)
    if not provider:
        return StandardResponse.error(message="未配置有效的模型提供商")
    if provider.usage == ModelUsage.EMBEDDING:
        return StandardResponse.error(message="当前模型提供商仅支持向量化，无法生成会话标题")
    if not provider.is_active:
        return StandardResponse.error(message="对话模型提供商已被禁用，无法生成会话标题")

    title = await generate_session_title(
        uid=uid,
        session_id=request.session_id,
        first_message=request.first_message,
        api_key=provider.api_key,
        base_url=provider.base_url,
        model_id=cfg.provider.model_id,
        protocol=getattr(provider, "protocol", "openai"),
        max_tokens=cfg.provider.max_tokens,
    )

    return StandardResponse.success(data={"title": title}, message="标题生成成功")


@router.get("/sessions/history")
async def get_session_history(
    session_id: str,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    offset = (page - 1) * size
    messages = await message_crud.get_history_paged(db, session_id=session_id, uid=uid, limit=size, offset=offset)

    # 倒序取出，正序返回
    messages.reverse()

    data = [MessageResponse.model_validate(m) for m in messages]
    return StandardResponse.success(data=data, message="会话历史记录获取成功")


@router.websocket("/ws")
async def chat_websocket(
    websocket: WebSocket,
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
    current_session_id = None

    async def run_chat(message_text, session_id, attachments=None, request_id=None):
        nonlocal active_task
        try:
            async with AsyncSessionLocal() as db:
                async for response in ws_chat_adapter.chat(
                    db=db,
                    message=message_text,
                    uid=uid,
                    session_id=session_id,
                    attachments=attachments,
                    request_id=request_id,
                ):
                    await websocket.send_json(response)
        except RuntimeError as e:
            # 拦截断开连接后的发送错误
            if "websocket.send" in str(e) and "websocket.close" in str(e):
                logger.bind(uid=uid, session_id=session_id).info("用户已断开连接，调度器终止")
            else:
                logger.bind(uid=uid, session_id=session_id).error(f"WebSocket 任务运行时错误: {e}")
        except asyncio.CancelledError:
            logger.bind(uid=uid, session_id=session_id).info("用户已断开连接，调度器终止")
            raise
        except Exception:
            logger.bind(uid=uid, session_id=session_id).exception("WebSocket 任务发生异常")
            try:
                await websocket.send_json({"type": "error", "message": "Internal server error"})
            except Exception:
                pass
        finally:
            active_task = None

    try:
        while True:
            # 接收 JSON 消息
            data = await websocket.receive_json()
            message = data.get("message")
            session_id = data.get("session_id")
            attachments = data.get("attachments")
            request_id = data.get("request_id")

            action = data.get("action")

            if action == "abort":
                if active_task and not active_task.done():
                    active_task.cancel()
                    logger.bind(uid=uid, session_id=session_id).info("接收到中止信号，生成任务已取消")
                continue

            if not message and not attachments:
                await websocket.send_json({"error": "Message or attachments is required"})
                continue

            # 会话 ID 生成与解析逻辑
            if not session_id:
                # 如果没有活跃任务，或者上次任务已结束，且收到 null，则视为开启新会话
                if not active_task or active_task.done():
                    current_session_id = str(uuid.uuid4())
                    # 立即推送给前端，确保其能同步状态
                    await websocket.send_json({"type": "session_id", "session_id": current_session_id})

                # 使用当前确定的 ID（无论是刚生成的还是之前正在用的）
                session_id = current_session_id
            else:
                # 前端明确传了 ID，则更新当前上下文 ID
                current_session_id = session_id

            # 如果当前已有任务在运行，新消息仅需保存到数据库
            if active_task and not active_task.done():
                from app.core.utils.dispatcher.save_initial_message import save_initial_message

                async with AsyncSessionLocal() as db:
                    profile = await profile_crud.get_active(db)
                    await save_initial_message(
                        db,
                        session_id,
                        uid,
                        profile,
                        message,
                        attachments,
                    )
                logger.bind(uid=uid, session_id=session_id).info(f"会话 {session_id} 存在活跃任务，消息已保存至数据库以待动态追加。")
            else:
                # 否则启动新的调度任务
                active_task = asyncio.create_task(run_chat(message, session_id, attachments, request_id))

    except WebSocketDisconnect:
        # 连接正常关闭
        if active_task and not active_task.done():
            active_task.cancel()
    except Exception:
        # 异常处理
        logger.bind(uid=uid).exception("聊天 WebSocket 发生异常")
        try:
            await websocket.send_json({"error": "Internal server error"})
        except Exception:
            pass
        if active_task and not active_task.done():
            active_task.cancel()
        await websocket.close()
    finally:
        # 确保任务被取消
        if active_task and not active_task.done():
            active_task.cancel()
