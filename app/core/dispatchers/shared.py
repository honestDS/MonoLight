import copy
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND
from app.core.context import ContextManager
from app.core.exceptions import LLMException
from app.core.utils.dispatcher.helpers import get_multimodal_from_entry, resolve_chat_params
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage


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
        additional_system_prompt: str | None = None,
    ) -> None:
        dispatcher_module = import_module("app.core.dispatcher")
        validate_profile = getattr(dispatcher_module, "validate_profile_and_cfg", validate_profile_and_cfg)
        select_chat_channel = getattr(dispatcher_module, "select_channel", select_channel)
        context_manager = getattr(dispatcher_module, "ContextManager", ContextManager)
        del additional_system_prompt

        cfg = await validate_profile(db, profile)
        chat_channel = cfg.channel.chat_channel
        selection = await select_chat_channel(db, chat_channel, "CHAT", call_context="chat_preflight", cursor_key=None, log_selection=False)
        if not selection:
            raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

        _chat_channel_obj, model_entry, _channel_rule = selection
        img_understanding, audio_understanding, video_understanding = get_multimodal_from_entry(model_entry)
        chat_params = resolve_chat_params(model_entry, chat_channel)

        validation_msg = InternalMessage.from_user_input(
            content=copy.deepcopy(message),
            attachments=copy.deepcopy(attachments),
        )
        if validation_msg.attachments or isinstance(validation_msg.content, list):
            validation_msg = MessageAssembler.assemble(
                validation_msg,
                image_understanding=img_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=False,
            )

        context_manager.validate_request_capacity(
            context_window_k=chat_params["context_window_k"],
            max_tokens=chat_params["max_tokens"],
        )
