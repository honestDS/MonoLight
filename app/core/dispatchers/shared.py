import copy
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND
from app.core.context import ContextManager
from app.core.exceptions import LLMException
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher.helpers import get_multimodal_from_entry, resolve_chat_params
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.core.utils.dispatcher.markdown_instruction import build_user_runtime_instructions
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole


class DispatcherValidationMixin:
    @classmethod
    async def validate_initial_message_before_save(
        cls,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        profile,
        attachments: list[str] | None = None,
    ) -> None:
        dispatcher_module = import_module("app.core.dispatcher")
        validate_profile = getattr(dispatcher_module, "validate_profile_and_cfg", validate_profile_and_cfg)
        select_chat_channel = getattr(dispatcher_module, "select_channel", select_channel)
        build_prompt = getattr(dispatcher_module, "build_system_prompt", build_system_prompt)
        get_profile_tools = getattr(dispatcher_module, "get_tools_for_profile", get_tools_for_profile)
        build_runtime_instructions = getattr(dispatcher_module, "build_user_runtime_instructions", build_user_runtime_instructions)
        context_manager = getattr(dispatcher_module, "ContextManager", ContextManager)

        cfg = await validate_profile(db, profile)
        chat_channel = cfg.channel.chat_channel
        selection = await select_chat_channel(db, chat_channel, "CHAT", call_context="chat_preflight", cursor_key=None, log_selection=False)
        if not selection:
            raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

        chat_channel_obj, model_entry, _channel_rule = selection
        img_understanding, audio_understanding, video_understanding = get_multimodal_from_entry(model_entry)
        chat_params = resolve_chat_params(model_entry, chat_channel)
        system_prompt = await build_prompt(db, profile)
        tools, _allowed_knowledge_base_ids = await get_profile_tools(db, profile)

        validation_msg = InternalMessage(role=MessageRole.USER, content=copy.deepcopy(message), attachments=copy.deepcopy(attachments))
        user_runtime_instructions = await build_runtime_instructions(db, session_id)
        if validation_msg.attachments or isinstance(validation_msg.content, list):
            validation_msg = MessageAssembler.assemble(
                validation_msg,
                image_understanding=img_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=False,
            )

        context_manager.validate_latest_user_message_budget(
            message=validation_msg,
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
            system_tokens=estimate_tokens(system_prompt) + estimate_tokens(user_runtime_instructions),
            tools=tools,
        )
