import asyncio
import json
import time
import uuid
import weakref
from dataclasses import dataclass, field

from fastapi import (
    APIRouter,
    Depends,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.adapters.chat_web import web_chat_adapter
from app.adapters.chat_ws import ws_chat_adapter
from app.core.channel_router import select_channel
from app.core.constants import (
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_CHAT_MESSAGE_OR_ATTACHMENTS_REQUIRED,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_NO_VALID_CHANNEL,
    ERR_SESSION_GUIDANCE_EXTERNAL_ONLY,
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_NOT_FOUND,
    ERR_SESSION_READ_ONLY,
    GUIDANCE_MESSAGE_PREFIX,
    GUIDANCE_MESSAGE_SUFFIX,
    MSG_BACKGROUND_TASK_DETAIL_SUCCESS,
    MSG_BACKGROUND_TASK_LIST_SUCCESS,
    MSG_MESSAGE_LIST_SUCCESS,
    MSG_SESSION_CLEARED,
    MSG_SESSION_GUIDANCE_CREATED,
    MSG_SESSION_LIST_SUCCESS,
    MSG_SESSION_UPDATED,
    MSG_TITLE_GENERATED,
)
from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.dispatcher import ChatDispatcher, format_exception_message
from app.core.exceptions import BaseBusinessException, ForbiddenException, LLMException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.i18n.locale import normalize_locale
from app.core.log import (
    channel_log_extra,
    get_logger,
    reset_system_log_locale,
    set_system_log_locale,
)
from app.core.profile_selection import resolve_profile_for_session
from app.core.profile_validation import get_validated_profile_for_assignment
from app.core.security import get_current_user
from app.core.session_cleanup import delete_session_data
from app.core.session_notifier import session_notifier
from app.core.session_reply_queue.manager import build_input_queued_event, is_submission_queued, session_reply_queue_manager
from app.core.session_source import default_show_tool_calls_for_source
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.session import ensure_web_session_writable, generate_session_title
from app.models.background_task import BackgroundTaskResponse
from app.models.channel import ChannelConfig, resolve_model_protocol
from app.models.message import (
    ChatCompletionRequest,
    MessageResponse,
    MessageRole,
)
from app.models.session import ChatSession
from app.providers.database import AsyncSessionLocal, get_db
from app.schemas.response import (
    LLMChoice,
    LLMChoiceMessage,
    LLMResponse,
    StandardResponse,
)

logger = get_logger(__name__)


router = APIRouter(prefix="/chat", tags=["Chat"], dependencies=[Depends(get_current_user)])


def _event_matches_session(event: object, session_id: str | None, *, require_session_id: bool = False) -> bool:
    if not session_id or not isinstance(event, dict):
        return False
    event_session_id = event.get("session_id")
    if event_session_id is None:
        return not require_session_id
    return event_session_id == session_id


async def _http_event_stream(
    db: AsyncSession,
    message: str | None,
    uid: str | None,
    session_id: str,
    attachments: list | None,
    request_id: str | None,
):
    async for event in web_chat_adapter.chat_stream(
        db=db,
        message=message,
        uid=uid,
        session_id=session_id,
        attachments=attachments,
        request_id=request_id,
    ):
        yield json.dumps(event, ensure_ascii=False) + "\n"


async def _create_new_web_session_with_profile_override(
    db: AsyncSession,
    session_id: str,
    uid: str | None,
    source: str,
    profile_override_id: int | None,
    show_tool_calls: bool | None = None,
) -> None:
    if profile_override_id is None and show_tool_calls is None:
        return

    existing_session = await session_crud.get_by_session_id(db, session_id)
    if existing_session:
        if existing_session.uid != uid:
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
        return

    profile = None
    if profile_override_id is not None:
        profile = await get_validated_profile_for_assignment(
            db,
            profile_id=profile_override_id,
            uid=uid,
        )
    db.add(
        ChatSession(
            session_id=session_id,
            uid=uid,
            profile_override_id=profile.id if profile else None,
            source=source,
            reply_target_source=source,
            show_tool_calls=show_tool_calls if show_tool_calls is not None else default_show_tool_calls_for_source(source),
        )
    )
    await db.commit()


@dataclass
class _WebSocketChatState:
    active_task: asyncio.Task | None = None
    active_tasks: weakref.WeakSet = field(default_factory=weakref.WeakSet)
    current_session_id: str | None = None
    notifier_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)


class NewSessionProfileSetting(BaseModel):
    profile_override_id: int | None = Field(default=None, gt=0)
    show_tool_calls: bool | None = None


async def _cancel_websocket_chat_tasks(state: _WebSocketChatState):
    """取消所有当前任务及子任务并等待其结束"""
    tasks_to_await = []
    if state.active_task and not state.active_task.done():
        state.active_task.cancel()
        tasks_to_await.append(state.active_task)

    for task in list(state.active_tasks):
        if not task.done():
            task.cancel()
            tasks_to_await.append(task)

    if tasks_to_await:
        # 等待所有任务完成取消过程，忽略 CancelledError
        await asyncio.gather(*tasks_to_await, return_exceptions=True)


async def _run_websocket_chat(
    websocket: WebSocket,
    state: _WebSocketChatState,
    uid: str | None,
    message_text,
    session_id,
    attachments=None,
    request_id=None,
):
    running_task = asyncio.current_task()
    state.active_tasks.clear()
    try:
        async with AsyncSessionLocal() as db:
            await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=uid,
            )
            async for response in ws_chat_adapter.chat(
                db=db,
                message=message_text,
                uid=uid,
                session_id=session_id,
                attachments=attachments,
                request_id=request_id,
                active_tasks=state.active_tasks,
            ):
                if session_id != state.current_session_id:
                    continue
                if not _event_matches_session(response, session_id):
                    continue
                await websocket.send_json(response)
    except BaseBusinessException as exc:
        if session_id == state.current_session_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": t(exc.message, **exc.kwargs),
                    "session_id": session_id,
                    "request_id": request_id,
                }
            )
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
        if session_id == state.current_session_id:
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": t(ERR_INTERNAL_SERVER_ERROR),
                        "session_id": session_id,
                        "request_id": request_id,
                    }
                )
            except Exception:
                pass
    finally:
        if state.active_task is running_task:
            state.active_task = None


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
        await _create_new_web_session_with_profile_override(
            db,
            session_id=new_session_id,
            uid=uid,
            source="http",
            profile_override_id=request.profile_override_id,
            show_tool_calls=request.show_tool_calls,
        )
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

    if request.stream:
        return StreamingResponse(
            _http_event_stream(
                db,
                request.message,
                uid,
                request.session_id,
                request.attachments,
                request.request_id,
            ),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 使用适配器处理对话请求
    return await web_chat_adapter.chat(
        db=db,
        message=request.message,
        uid=uid,
        session_id=request.session_id,
        attachments=request.attachments,
        request_id=request.request_id,
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
                "uid": row.uid,
                "last_active": row.last_active.strftime("%Y-%m-%d %H:%M:%S") if row.last_active else None,
                "username": row.username,
                "title": row.title,
                "enable_markdown": row.enable_markdown,
                "show_tool_calls": row.show_tool_calls,
                "profile_id": row.profile_id,
                "profile_override_id": row.profile_override_id,
                "source": row.source or "http",
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else None,
                "llm_request_metadata": row.llm_request_metadata,
            }
        )
    return StandardResponse.success(data=data, message=MSG_SESSION_LIST_SUCCESS)


@router.post("/sessions/delete")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)

    deleted = await delete_session_data(
        db,
        session_id=session_id,
        uid=uid,
        is_admin=is_admin,
    )
    if not deleted:
        await db.rollback()
        return StandardResponse.error(code=404, message=ERR_SESSION_NOT_FOUND)

    await db.commit()
    return StandardResponse.success(message=MSG_SESSION_CLEARED)


class SessionSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    enable_markdown: bool | None = None
    show_tool_calls: bool | None = None
    profile_override_id: int | None = Field(default=None, gt=0)


class SessionGuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    content: str = Field(max_length=10000)


@router.post("/sessions/guidance")
async def create_session_guidance(
    request: SessionGuidanceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    session = await session_crud.get_by_session_id(db, request.session_id)
    if not session:
        return StandardResponse.error(message=ERR_SESSION_NOT_FOUND)
    if session.uid != uid:
        return StandardResponse.error(message=ERR_SESSION_NO_PERMISSION)
    source = session.source or "http"
    if source in {"http", "ws"}:
        return StandardResponse.error(code=403, message=ERR_SESSION_GUIDANCE_EXTERNAL_ONLY)

    content = request.content.strip()
    if not content:
        return StandardResponse.error(message=ERR_CHAT_MESSAGE_OR_ATTACHMENTS_REQUIRED)

    profile_id = session.profile_id
    if profile_id is None:
        profile_id = await message_crud.get_latest_session_profile_id(
            db,
            session_id=session.session_id,
            uid=uid,
        )
    row = await message_crud.create_guidance(
        db,
        session_id=session.session_id,
        uid=uid,
        profile_id=profile_id if profile_id is not None else -1,
        content=f"{GUIDANCE_MESSAGE_PREFIX}{content}{GUIDANCE_MESSAGE_SUFFIX}",
    )
    return StandardResponse.success(
        data=MessageResponse.model_validate(row),
        message=MSG_SESSION_GUIDANCE_CREATED,
    )


@router.post("/sessions/setting")
async def update_session_setting(
    request: SessionSettingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    is_admin = getattr(current_user, "is_superuser", False)

    session = await session_crud.get_by_session_id(db, request.session_id)
    if not session:
        return StandardResponse.error(message=ERR_SESSION_NOT_FOUND)

    if not is_admin and session.uid != uid:
        return StandardResponse.error(message=ERR_SESSION_NO_PERMISSION)
    if request.enable_markdown is not None and session.source not in {"http", "ws"}:
        return StandardResponse.error(code=403, message=ERR_SESSION_READ_ONLY)

    if request.enable_markdown is not None:
        session.enable_markdown = request.enable_markdown
    if request.show_tool_calls is not None:
        session.show_tool_calls = request.show_tool_calls
    if "profile_override_id" in request.model_fields_set:
        if request.profile_override_id is None:
            session.profile_override_id = None
        else:
            profile = await get_validated_profile_for_assignment(
                db,
                profile_id=request.profile_override_id,
                uid=session.uid,
            )
            session.profile_override_id = profile.id
    await db.commit()

    return StandardResponse.success(message=MSG_SESSION_UPDATED)


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

    try:
        await ensure_web_session_writable(
            db,
            session_id=request.session_id,
            uid=uid,
        )
    except BaseBusinessException as exc:
        return StandardResponse.error(code=exc.code, message=exc.message)

    profile = await resolve_profile_for_session(db, uid=uid, session_id=request.session_id)
    if not profile:
        return StandardResponse.error(message=ERR_NO_VALID_CHANNEL)

    # 从 chat_channel 中选择一个可用的渠道来生成标题
    channel_cfg = (profile.configs or {}).get("channel", {})
    chat_channel_raw = channel_cfg.get("chat_channel")
    if not chat_channel_raw:
        return StandardResponse.error(message=ERR_NO_VALID_CHANNEL)

    try:
        chat_channel = ChannelConfig.model_validate(chat_channel_raw)
    except Exception:
        return StandardResponse.error(message=ERR_NO_VALID_CHANNEL)

    selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation", cursor_key=None)
    if not selection:
        return StandardResponse.error(message=ERR_NO_VALID_CHANNEL)

    excluded_priorities: set[int] = set()

    while True:
        channel, model_entry, _rule = selection
        try:
            await db.commit()
            title = await generate_session_title(
                uid=uid,
                session_id=request.session_id,
                first_message=request.first_message,
                api_key=channel.get_decrypted_api_key(),
                base_url=channel.base_url,
                model_id=model_entry["model_id"],
                protocol=resolve_model_protocol(model_entry),
                max_tokens=model_entry.get("max_tokens") or 200,
                raise_on_error=True,
                http_proxy=get_channel_http_proxy(channel),
                custom_headers=get_model_custom_headers(model_entry),
            )
            return StandardResponse.success(data={"title": title}, message=MSG_TITLE_GENERATED)
        except LLMException as e:
            # 仅 LLM 调用相关异常做降级；其他异常向上抛出，避免掩盖真实问题
            excluded_priorities.add(_rule.priority)
            logger.bind(
                uid=uid,
                session_id=request.session_id,
                **channel_log_extra(channel, model_entry),
            ).warning(t("LOG_TITLE_CHANNEL_FAILED", error=format_exception_message(e)))

        selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation_retry", excluded_priorities=excluded_priorities, cursor_key=None)
        if not selection:
            return StandardResponse.error(message=ERR_NO_VALID_CHANNEL)


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
    return StandardResponse.success(data=data, message=MSG_BACKGROUND_TASK_LIST_SUCCESS)


@router.get("/background-tasks/{task_id}")
async def get_background_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    uid = getattr(current_user, "uid", None)
    task = await background_task_crud.get_user_task(db, task_id=task_id, uid=uid)
    if not task:
        return StandardResponse.error(code=404, message=ERR_BACKGROUND_TASK_NOT_FOUND)
    return StandardResponse.success(data=BackgroundTaskResponse.model_validate(task), message=MSG_BACKGROUND_TASK_DETAIL_SUCCESS)


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
    session = await session_crud.get_by_session_id(db, session_id)
    messages = await message_crud.get_history_paged(
        db,
        session_id=session_id,
        uid=uid,
        limit=size,
        offset=offset,
        include_tool_messages=session.show_tool_calls if session else True,
    )

    # 倒序取出，正序返回
    messages.reverse()

    data = [MessageResponse.model_validate(m) for m in messages]
    return StandardResponse.success(data=data, message=MSG_MESSAGE_LIST_SUCCESS)


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
    state = _WebSocketChatState()

    try:
        while True:
            receive_task = asyncio.create_task(websocket.receive_json())
            notify_task = asyncio.create_task(state.notifier_queue.get())
            done, pending = await asyncio.wait({receive_task, notify_task}, return_when=asyncio.FIRST_COMPLETED)
            for pending_task in pending:
                pending_task.cancel()

            if notify_task in done:
                event = notify_task.result()
                if _event_matches_session(event, state.current_session_id, require_session_id=True):
                    if event.get("type") == "audit_tool_results_update":
                        async with AsyncSessionLocal() as db:
                            session = await session_crud.get_by_session_id(db, state.current_session_id)
                        if session and not session.show_tool_calls:
                            continue
                    await websocket.send_json(event)
                continue

            # 接收 JSON 消息
            data = receive_task.result()
            message = data.get("message")
            session_id = data.get("session_id")
            attachments = data.get("attachments")
            request_id = data.get("request_id")
            profile_override_id = data.get("profile_override_id")

            action = data.get("action")

            if action == "abort":
                await _cancel_websocket_chat_tasks(state)
                logger.bind(uid=uid, session_id=session_id).info(t("LOG_CHAT_WS_ABORT_CANCELLED"))
                continue

            if not message and not attachments:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": t(ERR_CHAT_MESSAGE_OR_ATTACHMENTS_REQUIRED),
                        "session_id": state.current_session_id,
                        "request_id": request_id,
                    }
                )
                continue

            # 会话 ID 解析与切换逻辑
            old_session_id = state.current_session_id
            if not session_id:
                # 如果收到空 session_id，决定是沿用当前会话还是开启新会话
                if not state.active_task or state.active_task.done():
                    try:
                        profile_setting = NewSessionProfileSetting.model_validate(
                            {
                                "profile_override_id": profile_override_id,
                                "show_tool_calls": data.get("show_tool_calls"),
                            }
                        )
                    except ValidationError as exc:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": str(exc),
                                "session_id": state.current_session_id,
                                "request_id": request_id,
                            }
                        )
                        continue

                    new_session_id = str(uuid.uuid4())
                    try:
                        async with AsyncSessionLocal() as db:
                            await _create_new_web_session_with_profile_override(
                                db,
                                session_id=new_session_id,
                                uid=uid,
                                source="ws",
                                profile_override_id=profile_setting.profile_override_id,
                                show_tool_calls=profile_setting.show_tool_calls,
                            )
                    except BaseBusinessException as exc:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": t(exc.message, **exc.kwargs),
                                "session_id": state.current_session_id,
                                "request_id": request_id,
                            }
                        )
                        continue

                    state.current_session_id = new_session_id
                    await websocket.send_json(
                        {
                            "type": "session_id",
                            "session_id": state.current_session_id,
                            "request_id": request_id,
                        }
                    )
                session_id = state.current_session_id
            else:
                state.current_session_id = session_id

            if old_session_id and old_session_id != state.current_session_id:
                await session_notifier.unregister(uid, old_session_id, state.notifier_queue)
            if state.current_session_id:
                await session_notifier.register(uid, state.current_session_id, state.notifier_queue)

            # 判断是否发生了会话切换
            is_session_switched = old_session_id is not None and session_id != old_session_id

            # 如果当前已有任务在运行
            if state.active_task and not state.active_task.done():
                if is_session_switched:
                    # 1. 切换会话场景：仅取消旧连接上的结果等待，持久化工作继续执行
                    await _cancel_websocket_chat_tasks(state)

                    logger.bind(uid=uid, old_session=old_session_id, new_session=session_id).info(t("LOG_CHAT_WS_SESSION_SWITCHED"))
                    state.active_task = asyncio.create_task(_run_websocket_chat(websocket, state, uid, message, session_id, attachments, request_id))
                else:
                    # 2. 同一会话场景：新消息仅需保存到数据库，由调度器动态追加
                    async with AsyncSessionLocal() as db:
                        try:
                            await ensure_web_session_writable(
                                db,
                                session_id=session_id,
                                uid=uid,
                            )
                            profile = await resolve_profile_for_session(db, uid=uid, session_id=session_id)
                            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
                        except BaseBusinessException as exc:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": t(exc.message, **exc.kwargs),
                                    "session_id": session_id,
                                    "request_id": request_id,
                                }
                            )
                            continue
                        _initial_message, work, submission_status = await session_reply_queue_manager.submit_user_message(
                            db,
                            uid=uid,
                            session_id=session_id,
                            profile=profile,
                            message=message,
                            attachments=attachments,
                            source="ws",
                            request_id=request_id,
                        )
                        if request_id and is_submission_queued(submission_status):
                            await websocket.send_json(
                                build_input_queued_event(
                                    session_id,
                                    request_id,
                                    work.id,
                                    submission_status,
                                )
                            )
                        logger.bind(uid=uid, session_id=session_id).info(t("LOG_WS_ACTIVE_TASK_SAVED", session_id=session_id))
            else:
                # 3. 无活跃任务：启动新任务
                state.active_task = asyncio.create_task(_run_websocket_chat(websocket, state, uid, message, session_id, attachments, request_id))

    except WebSocketDisconnect:
        # 连接正常关闭
        await _cancel_websocket_chat_tasks(state)
    except Exception:
        # 异常处理
        logger.bind(uid=uid).exception(t("LOG_CHAT_WS_EXCEPTION"))
        try:
            await websocket.send_json({"type": "error", "message": t(ERR_INTERNAL_SERVER_ERROR)})
        except Exception:
            pass
        await _cancel_websocket_chat_tasks(state)
        try:
            await websocket.close()
        except RuntimeError:
            pass
    finally:
        if log_locale_token is not None:
            reset_system_log_locale(log_locale_token)
        reset_current_locale(locale_token)

        # 连接断开只取消本地等待，不取消已经持久化的回复工作
        await _cancel_websocket_chat_tasks(state)

        if state.current_session_id:
            await session_notifier.unregister(uid, state.current_session_id, state.notifier_queue)
