"""对话调度器：渠道管理架构适配版

对话调度走 chat_channel：路由选择 -> 从 model_entry 读取参数 -> LLMClient 调用 -> 失败降级重试
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, MutableSet
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.channel_router import select_channel
from app.core.constants import (
    ERR_CHAT_CHANNEL_NOT_FOUND,
    ERR_LLM_EMPTY_RESPONSE,
)
from app.core.context import ContextManager
from app.core.crud.active_session import (
    active_session_crud,
)
from app.core.crud.profile import (
    profile_crud,
)
from app.core.crud.user import user_crud
from app.core.embedding.knowledge_base import is_embedding_profile_available
from app.core.exceptions import (
    ApiKeyException,
    BaseBusinessException,
    LLMException,
    ServerException,
)
from app.core.i18n import t
from app.core.log import (
    channel_log_extra,
    get_logger,
)
from app.core.prompts import (
    PROMPT_MAX_TURNS_REACHED,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.core.utils.dispatcher.mark_initial_message_processed import mark_initial_message_processed
from app.core.utils.dispatcher.markdown_instruction import append_session_markdown_instruction
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_initial_message import save_initial_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
)
from app.providers.llm.client import (
    LLMClient,
)
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


def _get_multimodal_from_entry(model_entry: dict) -> tuple[bool, bool, bool]:
    """从模型条目中提取多模态能力"""
    return (
        model_entry.get("image_understanding", False),
        model_entry.get("audio_understanding", False),
        model_entry.get("video_understanding", False),
    )


def _resolve_chat_params(model_entry: dict, chat_channel) -> dict:
    """从模型条目与对话渠道中解析对话参数。"""
    return {
        "temperature": model_entry.get("temperature") if model_entry.get("temperature") is not None else 0.7,
        "top_p": model_entry.get("top_p"),
        "max_tokens": model_entry.get("max_tokens") if model_entry.get("max_tokens") is not None else 2048,
        "chat_timeout": chat_channel.chat_timeout,
        "context_window_k": model_entry.get("context_window_k") if model_entry.get("context_window_k") is not None else 4,
    }


def _format_exception_message(exc: Exception) -> str:
    if isinstance(exc, BaseBusinessException):
        return t(exc.message, default=exc.message, **exc.kwargs)
    return str(exc)


def _reassemble_multimodal_messages(
    messages: list[InternalMessage],
    image_understanding: bool,
    audio_understanding: bool,
    video_understanding: bool,
) -> None:
    """按给定多模态能力就地重组消息列表中带附件的用户消息。

    依赖 MessageAssembler.assemble 的幂等性：可对已组装过的消息安全重复调用，
    用于降级换渠道后按新渠道能力重新组装附件内容。
    """
    for idx, m in enumerate(messages):
        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
            is_history = idx != len(messages) - 1
            messages[idx] = MessageAssembler.assemble(
                m,
                image_understanding=image_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=is_history,
            )


class ChatDispatcher:
    @staticmethod
    async def validate_initial_message_before_save(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        profile,
        attachments: list[str] | None = None,
    ) -> None:
        cfg = await validate_profile_and_cfg(db, profile)
        chat_channel = cfg.channel.chat_channel
        selection = await select_channel(db, chat_channel, "CHAT", call_context="chat_preflight", cursor_key=None, log_selection=False)
        if not selection:
            raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

        chat_channel_obj, model_entry, _channel_rule = selection
        img_understanding, audio_understanding, video_understanding = _get_multimodal_from_entry(model_entry)
        chat_params = _resolve_chat_params(model_entry, chat_channel)
        embedding_profile_available = await is_embedding_profile_available(db, profile)
        system_prompt = await build_system_prompt(db, profile, embedding_profile_available=embedding_profile_available)
        tools, _allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, embedding_profile_available=embedding_profile_available)

        initial_msg = InternalMessage(role=MessageRole.USER, content=message, attachments=attachments)
        await append_session_markdown_instruction(db, session_id, initial_msg)
        if initial_msg.attachments or isinstance(initial_msg.content, list):
            initial_msg = MessageAssembler.assemble(
                initial_msg,
                image_understanding=img_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=False,
            )

        ContextManager.validate_latest_user_message_budget(
            message=initial_msg,
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
            system_tokens=estimate_tokens(system_prompt),
            tools=tools,
        )

    @staticmethod
    async def dispatch(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ):
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_USER_MESSAGE", username=username, message=message, attachments=str(attachments)))

            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)

            # 1. 初始保存消息
            initial_msg = await save_initial_message(db, session_id, uid, profile, message, attachments)

            final_ai_content = ""
            turn_messages: list[InternalMessage] = []
            is_first_iter = True

            # 2. 分布式会话状态机
            while True:
                await active_session_crud.cleanup_expired_locks(db)
                lock_acquired = await active_session_crud.acquire_lock(db, session_id)

                if not lock_acquired:
                    logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_NON_STREAM_QUEUED", session_id=session_id))
                    return LLMResponse(
                        choices=[
                            LLMChoice(
                                message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=""),
                                finish_reason="queued",
                                created_at=time.time(),
                            )
                        ],
                        history=[],
                    ).model_dump()

                try:
                    cfg = await validate_profile_and_cfg(db, profile)

                    if is_first_iter:
                        await mark_initial_message_processed(db, initial_msg.id)

                    # ========== 渠道路由选择 ==========
                    chat_channel = cfg.channel.chat_channel
                    chat_cursor_key = f"{profile.id}:CHAT"
                    selection = await select_channel(db, chat_channel, "CHAT", call_context="chat_dispatch_non_stream", cursor_key=chat_cursor_key)
                    if not selection:
                        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

                    chat_channel_obj, model_entry, _channel_rule = selection
                    img_understanding, audio_understanding, video_understanding = _get_multimodal_from_entry(model_entry)
                    chat_params = _resolve_chat_params(model_entry, chat_channel)

                    embedding_profile_available = await is_embedding_profile_available(db, profile)

                    messages = await prepare_messages(
                        db,
                        session_id,
                        uid,
                        profile,
                        cfg,
                        initial_msg,
                        message,
                        is_first_iter,
                        context_window_k=chat_params["context_window_k"],
                        embedding_profile_available=embedding_profile_available,
                    )

                    # 重新组装带附件的多模态消息（使用模型实际的多模态能力）
                    for idx, m in enumerate(messages):
                        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
                            is_history = idx != len(messages) - 1
                            messages[idx] = MessageAssembler.assemble(
                                m,
                                image_understanding=img_understanding,
                                audio_understanding=audio_understanding,
                                video_understanding=video_understanding,
                                is_history=is_history,
                            )

                    tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, embedding_profile_available=embedding_profile_available)
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_NON_STREAM_ADDITIONAL_MESSAGES"))
                            current_turn = 0
                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
                            current_tools = None
                        else:
                            current_tools = tools

                        excluded_priorities: set[int] = set()
                        while True:
                            try:
                                request_messages = ContextManager.trim_messages_for_model_request(
                                    messages=messages,
                                    uid=uid,
                                    session_id=session_id,
                                    context_window_k=chat_params["context_window_k"],
                                    max_tokens=chat_params["max_tokens"],
                                    tools=current_tools,
                                )
                                response = await LLMClient.generate(
                                    api_key=chat_channel_obj.get_decrypted_api_key(),
                                    base_url=chat_channel_obj.base_url,
                                    model_id=model_entry["model_id"],
                                    messages=request_messages,
                                    temperature=chat_params["temperature"],
                                    top_p=chat_params["top_p"],
                                    max_tokens=chat_params["max_tokens"],
                                    tools=current_tools,
                                    protocol=getattr(chat_channel_obj, "protocol", "openai"),
                                    timeout=chat_params["chat_timeout"],
                                )
                                # 空响应（无内容且无工具调用）也视为渠道异常，纳入降级重试
                                ai_msg = response.message
                                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                                break
                            except ApiKeyException:
                                raise
                            except LLMException as exc:
                                # 仅捕获 LLM 调用相关异常（连接失败/超时/状态码错误/空响应等）做降级，
                                # 组装、协议转换或代码缺陷类异常向上抛出，避免掩盖真实问题
                                excluded_priorities.add(_channel_rule.priority)
                                logger.bind(
                                    uid=uid,
                                    session_id=session_id,
                                    **channel_log_extra(chat_channel_obj, model_entry),
                                ).warning(t("LOG_DISPATCHER_NON_STREAM_CHANNEL_FAILED", error=_format_exception_message(exc)))
                                if not chat_channel.retry_on_failure:
                                    raise
                                selection = await select_channel(db, chat_channel, "CHAT", call_context="chat_dispatch_non_stream_retry", excluded_priorities=excluded_priorities, cursor_key=chat_cursor_key)
                                if not selection:
                                    raise
                                chat_channel_obj, model_entry, _channel_rule = selection
                                img_understanding, audio_understanding, video_understanding = _get_multimodal_from_entry(model_entry)
                                chat_params = _resolve_chat_params(model_entry, chat_channel)
                                # 降级换渠道后，上下文必须按新模型的 context_window_k 重新构造并压缩
                                messages = await prepare_messages(
                                    db,
                                    session_id,
                                    uid,
                                    profile,
                                    cfg,
                                    initial_msg,
                                    message,
                                    is_first_iter,
                                    context_window_k=chat_params["context_window_k"],
                                    embedding_profile_available=embedding_profile_available,
                                )
                                _reassemble_multimodal_messages(messages, img_understanding, audio_understanding, video_understanding)

                        logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=current_turn, content=ai_msg.content or "[工具调用]"))

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)

                        await save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        if not ai_msg.tool_calls:
                            final_ai_content = ai_msg.content
                            new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_NON_STREAM_RESPONSE_CONTINUE"))
                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)

                            current_turn = 0
                            continue

                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await handle_parallel_tool_limit(db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages)
                            continue

                        sem = asyncio.Semaphore(cfg.tool.executor_max_workers)

                        async def wrapped_tool_call(tc):
                            async with sem:
                                task = asyncio.create_task(
                                    process_single_tool(
                                        tc,
                                        db,
                                        profile,
                                        cfg,
                                        messages,
                                        username,
                                        session_id,
                                        current_turn,
                                        uid,
                                        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                        context_window_k=chat_params["context_window_k"],
                                    )
                                )

                                if active_tasks is not None:
                                    active_tasks.add(task)

                                try:
                                    return await task
                                finally:
                                    if active_tasks is not None:
                                        active_tasks.discard(task)

                        tasks = [wrapped_tool_call(tc) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                if not new_user_msgs:
                    break

            return LLMResponse(
                choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=final_ai_content), finish_reason=True, created_at=time.time())],
                history=[m.model_dump(exclude_none=True) for m in turn_messages],
            ).model_dump()

        except BaseBusinessException:
            raise
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_ERROR"), exc_info=True)
            raise ServerException(message=str(e))

    @staticmethod
    async def dispatch_stream(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        request_id: str | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_USER_MESSAGE", username=username, message=message, attachments=str(attachments)))

            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)

            # 1. 初始保存消息
            initial_msg = await save_initial_message(db, session_id, uid, profile, message, attachments)

            turn_messages: list[InternalMessage] = []
            is_first_iter = True

            # 2. 分布式会话状态机
            while True:
                await active_session_crud.cleanup_expired_locks(db)
                lock_acquired = await active_session_crud.acquire_lock(db, session_id)

                if not lock_acquired:
                    logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_STREAM_QUEUED", session_id=session_id))
                    yield {"type": "content", "content": "", "turn": 0, "finish_reason": "queued", "request_id": request_id}
                    yield {"type": "done", "session_id": session_id, "history": [], "request_id": request_id}
                    return

                try:
                    yield {"type": "task_start", "request_id": request_id}

                    cfg = await validate_profile_and_cfg(db, profile)

                    if is_first_iter:
                        await mark_initial_message_processed(db, initial_msg.id)

                    # ========== 渠道路由选择 ==========
                    chat_channel = cfg.channel.chat_channel
                    chat_cursor_key = f"{profile.id}:CHAT"
                    selection = await select_channel(db, chat_channel, "CHAT", call_context="chat_dispatch_stream", cursor_key=chat_cursor_key)
                    if not selection:
                        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

                    chat_channel_obj, model_entry, _channel_rule = selection
                    img_understanding, audio_understanding, video_understanding = _get_multimodal_from_entry(model_entry)
                    chat_params = _resolve_chat_params(model_entry, chat_channel)

                    embedding_profile_available = await is_embedding_profile_available(db, profile)

                    messages = await prepare_messages(
                        db,
                        session_id,
                        uid,
                        profile,
                        cfg,
                        initial_msg,
                        message,
                        is_first_iter,
                        context_window_k=chat_params["context_window_k"],
                        embedding_profile_available=embedding_profile_available,
                    )

                    # 重新组装带附件的多模态消息
                    for idx, m in enumerate(messages):
                        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
                            is_history = idx != len(messages) - 1
                            messages[idx] = MessageAssembler.assemble(
                                m,
                                image_understanding=img_understanding,
                                audio_understanding=audio_understanding,
                                video_understanding=video_understanding,
                                is_history=is_history,
                            )

                    tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, embedding_profile_available=embedding_profile_available)
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            current_turn = 0
                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
                            current_tools = None
                        else:
                            current_tools = tools

                        current_tool_calls_map = {}
                        current_content_chunks = []

                        response_id = str(uuid.uuid4())

                        excluded_priorities: set[int] = set()
                        while True:
                            emitted_chunk = False
                            try:
                                request_messages = ContextManager.trim_messages_for_model_request(
                                    messages=messages,
                                    uid=uid,
                                    session_id=session_id,
                                    context_window_k=chat_params["context_window_k"],
                                    max_tokens=chat_params["max_tokens"],
                                    tools=current_tools,
                                )
                                async for chunk in LLMClient.generate_stream(
                                    api_key=chat_channel_obj.get_decrypted_api_key(),
                                    base_url=chat_channel_obj.base_url,
                                    model_id=model_entry["model_id"],
                                    messages=request_messages,
                                    temperature=chat_params["temperature"],
                                    top_p=chat_params["top_p"],
                                    max_tokens=chat_params["max_tokens"],
                                    tools=current_tools,
                                    protocol=getattr(chat_channel_obj, "protocol", "openai"),
                                    timeout=chat_params["chat_timeout"],
                                ):
                                    choices = chunk.get("choices", [])
                                    if not choices:
                                        continue
                                    choice = choices[0]
                                    delta = choice.get("delta", {})

                                    content = delta.get("content")
                                    if content:
                                        emitted_chunk = True
                                        current_content_chunks.append(content)
                                        yield {
                                            "type": "content",
                                            "content": content,
                                            "turn": current_turn,
                                            "response_id": response_id,
                                            "request_id": request_id,
                                            "session_id": session_id,
                                        }

                                    tool_calls = delta.get("tool_calls")
                                    if tool_calls:
                                        emitted_chunk = True
                                        for tc in tool_calls:
                                            idx = tc.get("index", 0)
                                            if idx not in current_tool_calls_map:
                                                current_tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                                            if tc.get("id"):
                                                current_tool_calls_map[idx]["id"] = tc.get("id")
                                            if tc.get("function", {}).get("name"):
                                                current_tool_calls_map[idx]["name"] = tc.get("function", {}).get("name")
                                            if tc.get("function", {}).get("arguments"):
                                                current_tool_calls_map[idx]["arguments"] += tc.get("function", {}).get("arguments")

                                # 流式结束后若本轮未产出任何有效内容（既无文本也无工具调用），
                                # 视为空响应：此时尚未向前端推送过内容，可安全降级到下一优先级组重试
                                stream_text = "".join(current_content_chunks).strip()
                                stream_has_tool = any(v.get("name") for v in current_tool_calls_map.values())
                                if not stream_text and not stream_has_tool:
                                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                                break
                            except ApiKeyException:
                                raise
                            except LLMException as exc:
                                # 仅捕获 LLM 调用相关异常做降级；已向前端推送过内容则不可降级，直接抛出
                                if emitted_chunk:
                                    raise
                                excluded_priorities.add(_channel_rule.priority)
                                logger.bind(
                                    uid=uid,
                                    session_id=session_id,
                                    **channel_log_extra(chat_channel_obj, model_entry),
                                ).warning(t("LOG_DISPATCHER_STREAM_CHANNEL_FAILED", error=_format_exception_message(exc)))
                                if not chat_channel.retry_on_failure:
                                    raise
                                selection = await select_channel(db, chat_channel, "CHAT", call_context="chat_dispatch_stream_retry", excluded_priorities=excluded_priorities, cursor_key=chat_cursor_key)
                                if not selection:
                                    raise
                                chat_channel_obj, model_entry, _channel_rule = selection
                                img_understanding, audio_understanding, video_understanding = _get_multimodal_from_entry(model_entry)
                                chat_params = _resolve_chat_params(model_entry, chat_channel)
                                # 降级换渠道后，上下文必须按新模型的 context_window_k 重新构造并压缩
                                messages = await prepare_messages(
                                    db,
                                    session_id,
                                    uid,
                                    profile,
                                    cfg,
                                    initial_msg,
                                    message,
                                    is_first_iter,
                                    context_window_k=chat_params["context_window_k"],
                                    embedding_profile_available=embedding_profile_available,
                                )
                                _reassemble_multimodal_messages(messages, img_understanding, audio_understanding, video_understanding)
                                current_tool_calls_map = {}
                                current_content_chunks = []

                        final_content = "".join(current_content_chunks)
                        final_tool_calls = []
                        for idx, tc_data in sorted(current_tool_calls_map.items()):
                            if tc_data.get("name"):
                                args_dict = {}
                                if tc_data.get("arguments"):
                                    try:
                                        args_dict = json.loads(tc_data.get("arguments"))
                                    except Exception:
                                        pass
                                final_tool_calls.append(InternalToolCall(id=tc_data.get("id") or f"call_{idx}", name=tc_data.get("name"), arguments=args_dict))

                        if not final_tool_calls and not final_content.strip():
                            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

                        ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content=final_content if final_content else None, tool_calls=final_tool_calls if final_tool_calls else None)

                        logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=current_turn, content=ai_msg.content or "[工具调用]"))

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)

                        saved_msg = await save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        if saved_msg:
                            yield {
                                "type": "turn_end",
                                "response_id": response_id,
                                "content": saved_msg.content,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                        if not ai_msg.tool_calls:
                            new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)
                            current_turn = 0
                            continue

                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await handle_parallel_tool_limit(db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages)
                            continue

                        for tc in ai_msg.tool_calls:
                            yield {
                                "type": "tool_start",
                                "name": tc.name,
                                "arguments": tc.arguments,
                                "tool_call_id": tc.id,
                                "response_id": response_id,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                        sem = asyncio.Semaphore(cfg.tool.executor_max_workers)

                        async def wrapped_tool_call(tc):
                            async with sem:
                                task = asyncio.create_task(
                                    process_single_tool(
                                        tc,
                                        db,
                                        profile,
                                        cfg,
                                        messages,
                                        username,
                                        session_id,
                                        current_turn,
                                        uid,
                                        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                        context_window_k=chat_params["context_window_k"],
                                    )
                                )

                                if active_tasks is not None:
                                    active_tasks.add(task)

                                try:
                                    return await task
                                finally:
                                    if active_tasks is not None:
                                        active_tasks.discard(task)

                        tasks = [wrapped_tool_call(tc) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)
                            tool_name = next((tc.name for tc in ai_msg.tool_calls if tc.id == tool_res.tool_call_id), "unknown")
                            yield {
                                "type": "tool_end",
                                "name": tool_name,
                                "result": tool_res.content,
                                "tool_call_id": tool_res.tool_call_id,
                                "response_id": response_id,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                if not new_user_msgs:
                    break

            yield {"type": "done", "session_id": session_id, "history": [m.model_dump(exclude_none=True) for m in turn_messages], "request_id": request_id}

        except BaseBusinessException as bbe:
            yield {"type": "error", "message": t(bbe.message, **bbe.kwargs), "request_id": request_id}
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_STREAM_ERROR"), exc_info=True)
            yield {"type": "error", "message": str(e), "request_id": request_id}
