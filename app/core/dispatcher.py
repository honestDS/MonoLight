"""对话调度器：渠道管理架构适配版

对话调度走 chat_channel：路由选择 -> 从 model_entry 读取参数 -> LLMClient 调用 -> 失败降级重试
"""

import asyncio
import copy
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
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT,
    PROMPT_MAX_TURNS_REACHED,
)
from app.core.tools import TOOL_EXECUTOR_MAP, get_tools_for_profile
from app.core.tools.send_file_to_user import sanitize_files_to_user_result
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.core.utils.dispatcher.mark_initial_message_processed import mark_initial_message_processed
from app.core.utils.dispatcher.markdown_instruction import build_user_runtime_instructions
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
from app.providers.database import AsyncSessionLocal
from app.providers.llm.client import (
    LLMClient,
)
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)

BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES = {"send_file_to_user"}


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


def _extract_files_to_user(tool_responses: list[InternalMessage]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for tool_response in tool_responses:
        if not isinstance(tool_response.content, str):
            continue
        try:
            payload = json.loads(tool_response.content)
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "files_to_user":
            continue
        for file_item in payload.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            file_id = file_item.get("id")
            if not file_id or file_id in seen_ids:
                continue
            seen_ids.add(file_id)
            files.append(file_item)

    return files


def _filter_tool_output_messages(messages: list[InternalMessage]) -> list[InternalMessage]:
    filtered_messages: list[InternalMessage] = []
    for message in messages:
        if message.role == MessageRole.TOOL:
            continue
        if message.role == MessageRole.ASSISTANT and message.tool_calls:
            if not (message.content or "").strip():
                continue
            filtered_messages.append(message.model_copy(update={"tool_calls": None}))
            continue
        filtered_messages.append(message)

    return filtered_messages


async def _process_single_tool_with_isolated_db(
    tool_call: InternalToolCall,
    profile,
    cfg,
    messages: list[InternalMessage],
    username: str,
    session_id: str,
    turn: int,
    uid: str,
    *,
    allowed_knowledge_base_ids: list[int] | None = None,
    context_window_k: int = 4,
) -> InternalMessage:
    async with AsyncSessionLocal() as tool_db:
        return await process_single_tool(
            tool_call,
            tool_db,
            profile,
            cfg,
            messages,
            username,
            session_id,
            turn,
            uid,
            allowed_knowledge_base_ids=allowed_knowledge_base_ids,
            context_window_k=context_window_k,
        )


def _dump_output_history(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in messages]


def _dump_background_proactive_history(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in _filter_tool_output_messages(messages)]


def _get_tool_schema_name(schema: dict[str, Any]) -> str | None:
    name = schema.get("function", {}).get("name")
    return name if isinstance(name, str) else None


def _filter_background_proactive_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing_tool_names = sorted(tool_name for tool_name in BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES if tool_name not in TOOL_EXECUTOR_MAP)
    if missing_tool_names:
        raise ServerException(message=f"Background proactive tool is not registered: {', '.join(missing_tool_names)}")
    return [tool for tool in tools if _get_tool_schema_name(tool) in BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES]


def _get_unsupported_background_proactive_tool_names(tool_calls: list[InternalToolCall]) -> list[str]:
    return sorted({tool_call.name for tool_call in tool_calls if tool_call.name not in BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES or tool_call.name not in TOOL_EXECUTOR_MAP})


def _validate_background_proactive_tool_calls(tool_calls: list[InternalToolCall]) -> None:
    unsupported_tool_names = _get_unsupported_background_proactive_tool_names(tool_calls)
    if unsupported_tool_names:
        raise LLMException(message=f"Background proactive reply attempted unsupported tool calls: {', '.join(unsupported_tool_names)}")


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
    async def _generate_reply_from_history(
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile,
        call_context: str,
        allow_tools: bool,
        extra_messages: list[InternalMessage] | None = None,
    ) -> tuple[InternalMessage, list[InternalMessage]]:
        user = await user_crud.get_by_uid(db, uid)
        username = user.username if user else "Unknown"
        cfg = await validate_profile_and_cfg(db, profile)
        chat_channel = cfg.channel.chat_channel
        chat_cursor_key = f"{profile.id}:CHAT"
        selection = await select_channel(db, chat_channel, "CHAT", call_context=call_context, cursor_key=chat_cursor_key)
        if not selection:
            raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

        chat_channel_obj, model_entry, _channel_rule = selection
        chat_params = _resolve_chat_params(model_entry, chat_channel)
        embedding_profile_available = await is_embedding_profile_available(db, profile)
        messages = await prepare_messages(
            db,
            session_id,
            uid,
            profile,
            cfg,
            None,
            "",
            False,
            context_window_k=chat_params["context_window_k"],
            embedding_profile_available=embedding_profile_available,
        )
        if extra_messages:
            messages.extend(extra_messages)

        tools = None
        allowed_knowledge_base_ids = None
        if allow_tools:
            profile_tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, embedding_profile_available=embedding_profile_available, allow_background=False)
            background_tools = _filter_background_proactive_tools(profile_tools)
            tools = background_tools or None

        request_messages = ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid=uid,
            session_id=session_id,
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
            tools=tools,
        )
        response = await LLMClient.generate(
            api_key=chat_channel_obj.get_decrypted_api_key(),
            base_url=chat_channel_obj.base_url,
            model_id=model_entry["model_id"],
            messages=request_messages,
            temperature=chat_params["temperature"],
            top_p=chat_params["top_p"],
            max_tokens=chat_params["max_tokens"],
            tools=tools,
            protocol=getattr(chat_channel_obj, "protocol", "openai"),
            timeout=chat_params["chat_timeout"],
        )
        ai_msg = response.message
        if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

        if allow_tools and ai_msg.tool_calls:
            unsupported_tool_names = _get_unsupported_background_proactive_tool_names(ai_msg.tool_calls)
            if unsupported_tool_names:
                logger.bind(uid=uid, session_id=session_id, unsupported_tools=unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_RETRY"))
                correction_message = InternalMessage(
                    role=MessageRole.SYSTEM,
                    content=json.dumps(
                        {
                            "type": "background_proactive_tool_correction",
                            "instruction": BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
                            "unsupported_tool_calls": unsupported_tool_names,
                            "allowed_tool_calls": sorted(BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES),
                        },
                        ensure_ascii=False,
                    ),
                )
                retry_messages = ContextManager.trim_messages_for_model_request(
                    messages=[*messages, correction_message],
                    uid=uid,
                    session_id=session_id,
                    context_window_k=chat_params["context_window_k"],
                    max_tokens=chat_params["max_tokens"],
                    tools=tools,
                )
                retry_response = await LLMClient.generate(
                    api_key=chat_channel_obj.get_decrypted_api_key(),
                    base_url=chat_channel_obj.base_url,
                    model_id=model_entry["model_id"],
                    messages=retry_messages,
                    temperature=chat_params["temperature"],
                    top_p=chat_params["top_p"],
                    max_tokens=chat_params["max_tokens"],
                    tools=tools,
                    protocol=getattr(chat_channel_obj, "protocol", "openai"),
                    timeout=chat_params["chat_timeout"],
                )
                ai_msg = retry_response.message
                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                remaining_unsupported_tool_names = _get_unsupported_background_proactive_tool_names(ai_msg.tool_calls or [])
                if remaining_unsupported_tool_names:
                    logger.bind(uid=uid, session_id=session_id, unsupported_tools=remaining_unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_TEXT_ONLY"))
                    text_only_message = InternalMessage(
                        role=MessageRole.SYSTEM,
                        content=json.dumps(
                            {
                                "type": "background_proactive_text_only_fallback",
                                "instruction": BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    text_only_messages = ContextManager.trim_messages_for_model_request(
                        messages=[*messages, correction_message, text_only_message],
                        uid=uid,
                        session_id=session_id,
                        context_window_k=chat_params["context_window_k"],
                        max_tokens=chat_params["max_tokens"],
                        tools=None,
                    )
                    text_only_response = await LLMClient.generate(
                        api_key=chat_channel_obj.get_decrypted_api_key(),
                        base_url=chat_channel_obj.base_url,
                        model_id=model_entry["model_id"],
                        messages=text_only_messages,
                        temperature=chat_params["temperature"],
                        top_p=chat_params["top_p"],
                        max_tokens=chat_params["max_tokens"],
                        tools=None,
                        protocol=getattr(chat_channel_obj, "protocol", "openai"),
                        timeout=chat_params["chat_timeout"],
                    )
                    ai_msg = text_only_response.message
                    if ai_msg.tool_calls:
                        ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content=BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT)
                    if not (ai_msg.content or "").strip():
                        raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

        logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=0, content=ai_msg.content or "[工具调用]"))
        messages.append(ai_msg)
        turn_messages = [ai_msg]
        await save_assistant_message(db, session_id, uid, profile.id, ai_msg)
        if not allow_tools or not ai_msg.tool_calls:
            return ai_msg, turn_messages

        _validate_background_proactive_tool_calls(ai_msg.tool_calls)
        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
            raise LLMException(message=f"Background proactive reply attempted too many tool calls: {len(ai_msg.tool_calls)}")

        tool_responses = await asyncio.gather(
            *[
                _process_single_tool_with_isolated_db(
                    tool_call,
                    profile,
                    cfg,
                    messages,
                    username,
                    session_id,
                    0,
                    uid,
                    allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                    context_window_k=chat_params["context_window_k"],
                )
                for tool_call in ai_msg.tool_calls
            ]
        )

        files_to_user = _extract_files_to_user(tool_responses)
        for tool_response in tool_responses:
            await save_tool_response(db, session_id, uid, profile.id, tool_response, messages, turn_messages)

        final_request_messages = ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid=uid,
            session_id=session_id,
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
            tools=None,
        )
        final_response = await LLMClient.generate(
            api_key=chat_channel_obj.get_decrypted_api_key(),
            base_url=chat_channel_obj.base_url,
            model_id=model_entry["model_id"],
            messages=final_request_messages,
            temperature=chat_params["temperature"],
            top_p=chat_params["top_p"],
            max_tokens=chat_params["max_tokens"],
            tools=None,
            protocol=getattr(chat_channel_obj, "protocol", "openai"),
            timeout=chat_params["chat_timeout"],
        )
        final_msg = final_response.message
        if final_msg.tool_calls:
            raise LLMException(message="Background proactive final reply must not call tools")
        if not (final_msg.content or "").strip() and not files_to_user:
            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
        if files_to_user:
            final_msg.content = json.dumps(
                {
                    "type": "assistant_files",
                    "text": final_msg.content or "",
                    "files": files_to_user,
                },
                ensure_ascii=False,
            )

        logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=1, content=final_msg.content or ""))
        messages.append(final_msg)
        turn_messages.append(final_msg)
        await save_assistant_message(db, session_id, uid, profile.id, final_msg)
        return final_msg, turn_messages

    @staticmethod
    async def dispatch_proactive_reply(task_id: int) -> dict[str, Any]:
        from app.core.crud.background_task import background_task_crud
        from app.providers.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            task = await background_task_crud.get(db, task_id)
            if not task:
                raise ServerException(message="Background task not found")
            task_uid = task.uid
            task_session_id = task.session_id
            task_profile_id = task.profile_id

            await active_session_crud.cleanup_expired_locks(db)
            lock_acquired = await active_session_crud.acquire_lock(db, task_session_id)
            if not lock_acquired:
                return {
                    "uid": task_uid,
                    "session_id": task_session_id,
                    "content": "",
                    "history": [],
                    "deferred": True,
                }

            try:
                profile = await profile_crud.get_with_relations(db, task_profile_id)
                if not profile:
                    raise ServerException(message="Background task profile not found")
                ai_msg, turn_messages = await ChatDispatcher._generate_reply_from_history(
                    db,
                    uid=task_uid,
                    session_id=task_session_id,
                    profile=profile,
                    call_context="background_task_proactive_reply",
                    allow_tools=True,
                )
                return {
                    "uid": task_uid,
                    "session_id": task_session_id,
                    "content": ai_msg.content,
                    "history": _dump_background_proactive_history(turn_messages),
                }
            finally:
                await active_session_crud.release_lock(db, task_session_id)

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

        validation_msg = InternalMessage(role=MessageRole.USER, content=copy.deepcopy(message), attachments=copy.deepcopy(attachments))
        user_runtime_instructions = await build_user_runtime_instructions(db, session_id)
        if validation_msg.attachments or isinstance(validation_msg.content, list):
            validation_msg = MessageAssembler.assemble(
                validation_msg,
                image_understanding=img_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=False,
            )

        ContextManager.validate_latest_user_message_budget(
            message=validation_msg,
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
            system_tokens=estimate_tokens(system_prompt) + estimate_tokens(user_runtime_instructions),
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
            files_to_user: list[dict[str, Any]] = []
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

                        if not ai_msg.tool_calls and files_to_user:
                            ai_msg.content = json.dumps(
                                {
                                    "type": "assistant_files",
                                    "text": ai_msg.content or "",
                                    "files": files_to_user,
                                },
                                ensure_ascii=False,
                            )

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
                                    _process_single_tool_with_isolated_db(
                                        tc,
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

                        files_to_user.extend(_extract_files_to_user(tool_responses))
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
                history=_dump_output_history(turn_messages),
                files=files_to_user or None,
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
            files_to_user: list[dict[str, Any]] = []
            final_response_id: str | None = None
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
                        final_response_id = response_id

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
                        if not ai_msg.tool_calls and files_to_user:
                            ai_msg.content = json.dumps(
                                {
                                    "type": "assistant_files",
                                    "text": ai_msg.content or "",
                                    "files": files_to_user,
                                },
                                ensure_ascii=False,
                            )

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
                                    _process_single_tool_with_isolated_db(
                                        tc,
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

                        files_to_user.extend(_extract_files_to_user(tool_responses))
                        for tool_res in tool_responses:
                            await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)
                            tool_call = next((tc for tc in ai_msg.tool_calls if tc.id == tool_res.tool_call_id), None)
                            tool_name = tool_call.name if tool_call else "unknown"
                            yield {
                                "type": "tool_end",
                                "name": tool_name,
                                "result": sanitize_files_to_user_result(tool_res.content),
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

            yield {"type": "done", "session_id": session_id, "history": _dump_output_history(turn_messages), "files": files_to_user or None, "response_id": final_response_id, "request_id": request_id}

        except BaseBusinessException as bbe:
            yield {"type": "error", "message": t(bbe.message, **bbe.kwargs), "request_id": request_id}
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_STREAM_ERROR"), exc_info=True)
            yield {"type": "error", "message": str(e), "request_id": request_id}
