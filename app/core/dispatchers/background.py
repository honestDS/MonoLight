import asyncio
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND, ERR_LLM_EMPTY_RESPONSE
from app.core.context import ContextManager
from app.core.crud.active_session import active_session_crud
from app.core.crud.profile import profile_crud
from app.core.crud.user import user_crud
from app.core.embedding.knowledge_base import is_embedding_profile_available
from app.core.exceptions import LLMException, ServerException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import (
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher.helpers import (
    BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES,
    _dump_background_proactive_history,
    _extract_files_to_user,
    _filter_background_proactive_tools,
    _get_unsupported_background_proactive_tool_names,
    _process_single_tool_with_isolated_db,
    _resolve_chat_params,
    _validate_background_proactive_tool_calls,
)
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.message import InternalMessage, MessageRole
from app.providers.database import AsyncSessionLocal
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


class BackgroundDispatcherMixin:
    @classmethod
    async def _generate_reply_from_history(
        cls,
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

    @classmethod
    async def dispatch_proactive_reply(cls, task_id: int) -> dict[str, Any]:
        from app.core.crud.background_task import background_task_crud

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
                ai_msg, turn_messages = await cls._generate_reply_from_history(
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
