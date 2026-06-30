import asyncio
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_BACKGROUND_TASK_NOT_FOUND, ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE, ERR_LLM_EMPTY_RESPONSE
from app.core.context import ContextManager
from app.core.crud.active_session import active_session_crud
from app.core.crud.profile import profile_crud
from app.core.crud.user import user_crud
from app.core.exceptions import LLMException, ServerException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import (
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher.channel_call import generate_chat_with_fallback
from app.core.utils.dispatcher.helpers import (
    BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES,
    _dump_background_proactive_history,
    _extract_files_to_user,
    _filter_background_proactive_tools,
    _get_unsupported_background_proactive_tool_names,
    _process_single_tool_with_isolated_db,
    _validate_background_proactive_tool_calls,
)
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.message import InternalMessage, MessageRole
from app.providers.database import AsyncSessionLocal

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
        restrict_tools_to_background_allowlist: bool = True,
        reply_source: str = "background_task",
    ) -> tuple[InternalMessage, list[InternalMessage]]:
        user = await user_crud.get_by_uid(db, uid)
        username = user.username if user else "Unknown"
        cfg = await validate_profile_and_cfg(db, profile)
        chat_channel = cfg.channel.chat_channel
        chat_cursor_key = f"{profile.id}:CHAT"
        messages: list[InternalMessage] = []

        tools = None
        allowed_knowledge_base_ids = None
        allowed_tool_names = None
        if allow_tools:
            profile_tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, allow_background=False)
            if restrict_tools_to_background_allowlist:
                profile_tools = _filter_background_proactive_tools(profile_tools)
                allowed_tool_names = BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES
            else:
                allowed_tool_names = {tool["function"]["name"] for tool in profile_tools if isinstance(tool.get("function", {}).get("name"), str)}
            tools = profile_tools or None

        async def build_initial_request(chat_params):
            nonlocal messages
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
            )
            if extra_messages:
                messages.extend(extra_messages)
            return ContextManager.trim_messages_for_model_request(
                messages=messages,
                uid=uid,
                session_id=session_id,
                context_window_k=chat_params["context_window_k"],
                max_tokens=chat_params["max_tokens"],
                tools=tools,
            )

        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, allow_tools=allow_tools).info(t("LOG_PROACTIVE_REPLY_GENERATION_STARTED"))
        response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
            db,
            chat_channel=chat_channel,
            request_builder=build_initial_request,
            call_context=call_context,
            cursor_key=chat_cursor_key,
            uid=uid,
            session_id=session_id,
            tools=tools,
        )
        ai_msg = response.message

        if allow_tools and ai_msg.tool_calls:
            unsupported_tool_names = _get_unsupported_background_proactive_tool_names(ai_msg.tool_calls, allowed_tool_names=allowed_tool_names)
            if unsupported_tool_names:
                logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, unsupported_tools=unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_RETRY"))
                correction_message = InternalMessage(
                    role=MessageRole.SYSTEM,
                    content=json.dumps(
                        {
                            "type": "background_proactive_tool_correction",
                            "instruction": BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
                            "unsupported_tool_calls": unsupported_tool_names,
                            "allowed_tool_calls": sorted(allowed_tool_names or BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES),
                        },
                        ensure_ascii=False,
                    ),
                )

                def build_correction_request(retry_chat_params):
                    return ContextManager.trim_messages_for_model_request(
                        messages=[*messages, correction_message],
                        uid=uid,
                        session_id=session_id,
                        context_window_k=retry_chat_params["context_window_k"],
                        max_tokens=retry_chat_params["max_tokens"],
                        tools=tools,
                    )

                retry_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
                    db,
                    chat_channel=chat_channel,
                    request_builder=build_correction_request,
                    call_context=f"{call_context}_tool_correction",
                    cursor_key=chat_cursor_key,
                    uid=uid,
                    session_id=session_id,
                    tools=tools,
                )
                ai_msg = retry_response.message
                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                remaining_unsupported_tool_names = _get_unsupported_background_proactive_tool_names(ai_msg.tool_calls or [], allowed_tool_names=allowed_tool_names)
                if remaining_unsupported_tool_names:
                    logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, unsupported_tools=remaining_unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_TEXT_ONLY"))
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

                    def build_text_only_request(retry_chat_params):
                        return ContextManager.trim_messages_for_model_request(
                            messages=[*messages, correction_message, text_only_message],
                            uid=uid,
                            session_id=session_id,
                            context_window_k=retry_chat_params["context_window_k"],
                            max_tokens=retry_chat_params["max_tokens"],
                            tools=None,
                        )

                    text_only_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
                        db,
                        chat_channel=chat_channel,
                        request_builder=build_text_only_request,
                        call_context=f"{call_context}_text_only",
                        cursor_key=chat_cursor_key,
                        uid=uid,
                        session_id=session_id,
                        tools=None,
                        require_content=True,
                    )
                    ai_msg = text_only_response.message
                    if ai_msg.tool_calls:
                        ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content=BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT)
                    if not (ai_msg.content or "").strip():
                        raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=0, content=ai_msg.content or "[工具调用]"))
        messages.append(ai_msg)
        turn_messages = [ai_msg]
        await save_assistant_message(db, session_id, uid, profile.id, ai_msg)
        if not allow_tools or not ai_msg.tool_calls:
            return ai_msg, turn_messages

        _validate_background_proactive_tool_calls(ai_msg.tool_calls, allowed_tool_names=allowed_tool_names)
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
                    allow_background_submission=False,
                )
                for tool_call in ai_msg.tool_calls
            ]
        )

        files_to_user = _extract_files_to_user(tool_responses)
        for tool_response in tool_responses:
            await save_tool_response(db, session_id, uid, profile.id, tool_response, messages, turn_messages)

        def build_final_request(final_chat_params):
            return ContextManager.trim_messages_for_model_request(
                messages=messages,
                uid=uid,
                session_id=session_id,
                context_window_k=final_chat_params["context_window_k"],
                max_tokens=final_chat_params["max_tokens"],
                tools=None,
            )

        final_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
            db,
            chat_channel=chat_channel,
            request_builder=build_final_request,
            call_context=f"{call_context}_final",
            cursor_key=chat_cursor_key,
            uid=uid,
            session_id=session_id,
            tools=None,
            require_content_or_tools=True,
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

        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=1, content=final_msg.content or ""))
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
                raise ServerException(message=ERR_BACKGROUND_TASK_NOT_FOUND)
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
                if not profile or profile.uid != task_uid:
                    logger.bind(
                        task_id=task_id,
                        uid=task_uid,
                        session_id=task_session_id,
                        profile_id=task_profile_id,
                    ).error(t("LOG_BACKGROUND_TASK_PROFILE_UNAVAILABLE"))
                    raise ServerException(message=ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE)
                ai_msg, turn_messages = await cls._generate_reply_from_history(
                    db,
                    uid=task_uid,
                    session_id=task_session_id,
                    profile=profile,
                    call_context="background_task_proactive_reply",
                    allow_tools=True,
                    reply_source="background_task",
                )
                return {
                    "uid": task_uid,
                    "session_id": task_session_id,
                    "content": ai_msg.content,
                    "history": _dump_background_proactive_history(turn_messages),
                }
            finally:
                await active_session_crud.release_lock(db, task_session_id)
