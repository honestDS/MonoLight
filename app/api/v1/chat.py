import asyncio
import time
import uuid
import weakref

from fastapi import (
    APIRouter,
    Depends,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.core import constants
from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.i18n.locale import normalize_locale
from app.core.log import (
    get_logger,
    reset_system_log_locale,
    set_system_log_locale,
)
from app.core.security import get_current_user
from app.core.session_notifier import session_notifier
from app.core.utils.dispatcher.save_initial_message import save_initial_message
from app.core.utils.session import generate_session_title
from app.models.background_task import BackgroundTaskResponse
from app.models.channel import ChannelConfig
from app.models.message import (
    ChatCompletionRequest,
    MessageResponse,
    MessageRole,
)
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

    data = []
    for row in sessions:
        data.append(
            {
                "session_id": row.session_id,
                "last_active": row.last_active.strftime("%Y-%m-%d %H:%M:%S") if row.last_active else None,
                "username": row.username,
                "title": row.title,
                "enable_markdown": row.enable_markdown,
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else None,
            }
        )
    return StandardResponse.success(data=data, message=constants.MSG_SESSION_LIST_SUCCESS)


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
        return StandardResponse.error(code=404, message=constants.ERR_SESSION_NOT_FOUND)

    return StandardResponse.success(message=constants.MSG_SESSION_CLEARED)


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
        return StandardResponse.error(message=constants.ERR_SESSION_NOT_FOUND)

    if not is_admin and session.uid != uid:
        return StandardResponse.error(message=constants.ERR_SESSION_NO_PERMISSION)

    session.enable_markdown = request.enable_markdown
    await db.commit()

    return StandardResponse.success(message=constants.MSG_SESSION_UPDATED)


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

    profile = await profile_crud.get_active(db, uid=uid)
    if not profile:
        return StandardResponse.error(message=constants.ERR_NO_VALID_CHANNEL)

    # 从 chat_channel 中选择一个可用的渠道来生成标题
    channel_cfg = (profile.configs or {}).get("channel", {})
    chat_channel_raw = channel_cfg.get("chat_channel")
    if not chat_channel_raw:
        return StandardResponse.error(message=constants.ERR_NO_VALID_CHANNEL)

    try:
        chat_channel = ChannelConfig.model_validate(chat_channel_raw)
    except Exception:
        return StandardResponse.error(message=constants.ERR_NO_VALID_CHANNEL)

    from app.core.channel_router import select_channel
    from app.core.dispatcher import _format_exception_message
    from app.core.exceptions import LLMException
    from app.core.log import channel_log_extra

    selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation", cursor_key=None)
    if not selection:
        return StandardResponse.error(message=constants.ERR_NO_VALID_CHANNEL)

    excluded_priorities: set[int] = set()

    while True:
        channel, model_entry, _rule = selection
        try:
            title = await generate_session_title(
                uid=uid,
                session_id=request.session_id,
                first_message=request.first_message,
                api_key=channel.get_decrypted_api_key(),
                base_url=channel.base_url,
                model_id=model_entry["model_id"],
                protocol=getattr(channel, "protocol", "openai"),
                max_tokens=model_entry.get("max_tokens") or 200,
                raise_on_error=True,
            )
            return StandardResponse.success(data={"title": title}, message=constants.MSG_TITLE_GENERATED)
        except LLMException as e:
            # 仅 LLM 调用相关异常做降级；其他异常向上抛出，避免掩盖真实问题
            excluded_priorities.add(_rule.priority)
            logger.bind(
                uid=uid,
                session_id=request.session_id,
                **channel_log_extra(channel, model_entry),
            ).warning(t("LOG_TITLE_CHANNEL_FAILED", error=_format_exception_message(e)))

        selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation_retry", excluded_priorities=excluded_priorities, cursor_key=None)
        if not selection:
            return StandardResponse.error(message=constants.ERR_NO_VALID_CHANNEL)


@router.get("/background-tasks")
async def list_background_tasks(
    session_id: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    offset = (page - 1) * size
    tasks = await background_task_crud.list_user_tasks(db, uid=uid, session_id=session_id, skip=offset, limit=size)
    data = [BackgroundTaskResponse.model_validate(task) for task in tasks]
    return StandardResponse.success(data=data, message="background task list success")


@router.get("/background-tasks/{task_id}")
async def get_background_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    task = await background_task_crud.get_user_task(db, task_id=task_id, uid=uid)
    if not task:
        return StandardResponse.error(code=404, message="background task not found")
    return StandardResponse.success(data=BackgroundTaskResponse.model_validate(task), message="background task detail success")


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
    return StandardResponse.success(data=data, message=constants.MSG_MESSAGE_LIST_SUCCESS)


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
    # 从 query 获取 lang 并设置上下文
    lang_param = websocket.query_params.get("lang")
    locale_token = set_current_locale(normalize_locale(lang_param if lang_param is not None else ""))

    uid = getattr(current_user, "uid", None)
    log_locale_token = None
    async with AsyncSessionLocal() as db:
        settings = await system_setting_crud.get_runtime_settings(db)
        log_locale_token = set_system_log_locale(settings.log_locale)

    # 用于追踪当前是否有正在运行的调度任务
    active_task: asyncio.Task | None = None
    active_tasks = weakref.WeakSet()  # 使用弱引用集合记录子任务，防止内存泄漏
    current_session_id = None
    notifier_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def cancel_all_tasks():
        """取消所有当前任务及子任务并等待其结束"""
        tasks_to_await = []
        if active_task and not active_task.done():
            active_task.cancel()
            tasks_to_await.append(active_task)

        for t_sub in list(active_tasks):
            if not t_sub.done():
                t_sub.cancel()
                tasks_to_await.append(t_sub)

        if tasks_to_await:
            # 等待所有任务完成取消过程，忽略 CancelledError
            await asyncio.gather(*tasks_to_await, return_exceptions=True)

    async def run_chat(message_text, session_id, attachments=None, request_id=None):
        nonlocal active_task
        active_tasks.clear()
        try:
            async with AsyncSessionLocal() as db:
                async for response in ws_chat_adapter.chat(
                    db=db,
                    message=message_text,
                    uid=uid,
                    session_id=session_id,
                    attachments=attachments,
                    request_id=request_id,
                    active_tasks=active_tasks,
                ):
                    await websocket.send_json(response)
        except RuntimeError as e:
            # 拦截断开连接后的发送错误
            if "websocket.send" in str(e) and "websocket.close" in str(e):
                logger.bind(uid=uid, session_id=session_id).info(t("LOG_CHAT_WS_USER_DISCONNECTED"))
            else:
                logger.bind(uid=uid, session_id=session_id).error(t("LOG_CHAT_WS_RUNTIME_ERROR", error=str(e)))
        except asyncio.CancelledError:
            logger.bind(uid=uid, session_id=session_id).info(t("LOG_CHAT_WS_USER_DISCONNECTED"))
            raise
        except Exception:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_CHAT_WS_TASK_EXCEPTION"), exc_info=True)
            try:
                await websocket.send_json({"type": "error", "message": t(constants.ERR_INTERNAL_SERVER_ERROR), "request_id": request_id})
            except Exception:
                pass
        finally:
            active_task = None

    try:
        while True:
            receive_task = asyncio.create_task(websocket.receive_json())
            notify_task = asyncio.create_task(notifier_queue.get())
            done, pending = await asyncio.wait({receive_task, notify_task}, return_when=asyncio.FIRST_COMPLETED)
            for pending_task in pending:
                pending_task.cancel()

            if notify_task in done:
                await websocket.send_json(notify_task.result())
                continue

            # 接收 JSON 消息
            data = receive_task.result()
            message = data.get("message")
            session_id = data.get("session_id")
            attachments = data.get("attachments")
            request_id = data.get("request_id")

            action = data.get("action")

            if action == "abort":
                await cancel_all_tasks()
                logger.bind(uid=uid, session_id=session_id).info(t("LOG_CHAT_WS_ABORT_CANCELLED"))
                continue

            if not message and not attachments:
                await websocket.send_json({"type": "error", "message": t(constants.ERR_CHAT_MESSAGE_OR_ATTACHMENTS_REQUIRED), "request_id": request_id})
                continue

            # 会话 ID 解析与切换逻辑
            old_session_id = current_session_id
            if not session_id:
                # 如果收到空 session_id，决定是沿用当前会话还是开启新会话
                if not active_task or active_task.done():
                    current_session_id = str(uuid.uuid4())
                    await websocket.send_json({"type": "session_id", "session_id": current_session_id})
                session_id = current_session_id
            else:
                current_session_id = session_id

            if old_session_id and old_session_id != current_session_id:
                await session_notifier.unregister(uid, old_session_id, notifier_queue)
            if current_session_id:
                await session_notifier.register(uid, current_session_id, notifier_queue)

            # 判断是否发生了会话切换
            is_session_switched = old_session_id is not None and session_id != old_session_id

            # 如果当前已有任务在运行
            if active_task and not active_task.done():
                if is_session_switched:
                    # 1. 切换会话场景：取消旧任务，显式清理锁后再启动新会话任务
                    await cancel_all_tasks()

                    # 强制显式清理旧会话锁
                    await ws_chat_adapter.release_session_lock(old_session_id)

                    logger.bind(uid=uid, old_session=old_session_id, new_session=session_id).info(t("LOG_CHAT_WS_SESSION_SWITCHED"))
                    active_task = asyncio.create_task(run_chat(message, session_id, attachments, request_id))
                else:
                    # 2. 同一会话场景：新消息仅需保存到数据库，由调度器动态追加
                    async with AsyncSessionLocal() as db:
                        profile = await profile_crud.get_active(db, uid=uid)
                        try:
                            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
                        except BaseBusinessException as exc:
                            await websocket.send_json({"type": "error", "message": t(exc.message, **exc.kwargs), "request_id": request_id})
                            continue
                        await save_initial_message(
                            db,
                            session_id,
                            uid,
                            profile,
                            message,
                            attachments,
                        )
                        logger.bind(uid=uid, session_id=session_id).info(t("LOG_WS_ACTIVE_TASK_SAVED", session_id=session_id))
            else:
                # 3. 无活跃任务：启动新任务
                active_task = asyncio.create_task(run_chat(message, session_id, attachments, request_id))

    except WebSocketDisconnect:
        # 连接正常关闭
        await cancel_all_tasks()
    except Exception:
        # 异常处理
        logger.bind(uid=uid).exception(t("LOG_CHAT_WS_EXCEPTION"))
        try:
            await websocket.send_json({"type": "error", "message": t(constants.ERR_INTERNAL_SERVER_ERROR)})
        except Exception:
            pass
        await cancel_all_tasks()
        try:
            await websocket.close()
        except RuntimeError:
            pass
    finally:
        if log_locale_token is not None:
            reset_system_log_locale(log_locale_token)
        reset_current_locale(locale_token)

        # 确保任务被取消并释放锁
        await cancel_all_tasks()

        # 显式清理当前会话锁
        if current_session_id:
            await session_notifier.unregister(uid, current_session_id, notifier_queue)
            try:
                await ws_chat_adapter.release_session_lock(current_session_id)
            except Exception:
                logger.bind(uid=uid, session_id=current_session_id).error(t("LOG_CHAT_WS_RELEASE_LOCK_FAILED"), exc_info=True)
