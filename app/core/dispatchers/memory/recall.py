import uuid

from app.core.constants import LOG_MEMORY_RECALL_CHANNEL_FAILED
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.utils.dispatcher.helpers import (
    format_exception_message,
    get_multimodal_from_entry,
    reassemble_multimodal_messages,
    resolve_chat_params,
)
from app.models.message import InternalMessage

from .persistence import (
    append_once,
    load_dedupe_messages,
    save_and_execute_recall,
)
from .request import (
    build_correction_messages,
    fallback_channel,
    generate,
    prepare_request_messages,
    response_is_valid,
    select_initial_channel,
    update_output_metadata,
)
from .types import (
    MemoryRecallContext,
    MemoryRecallPrecheckResult,
    build_result,
    get_profile_id,
)

logger = get_logger(__name__)


def _log_failure(context: MemoryRecallContext, error_type: str) -> None:
    logger.bind(
        uid=context.uid,
        session_id=context.session_id,
        boundary=context.current_user_boundary_message_id,
        status="failed",
        error_type=error_type,
    ).warning("long-term memory recall precheck failed")


def _valid_entry_context(context: MemoryRecallContext) -> bool:
    return get_profile_id(context) is not None and isinstance(context.current_user_boundary_message_id, int) and not isinstance(context.current_user_boundary_message_id, bool) and context.current_user_boundary_message_id > 0


async def run_memory_recall_precheck(
    context: MemoryRecallContext,
) -> MemoryRecallPrecheckResult:
    if not _valid_entry_context(context):
        _log_failure(context, "invalid_context")
        return build_result(context, "failed", "invalid_context")

    try:
        assistant_message, tool_message, assistant_key, tool_key = await load_dedupe_messages(
            context,
        )
        if assistant_message is not None:
            if tool_message is not None:
                append_once(context.messages, assistant_message)
                append_once(context.turn_messages, assistant_message)
                append_once(context.messages, tool_message)
                append_once(context.turn_messages, tool_message)
                return build_result(context, "completed")
            if not context.chat_params:
                context.chat_params = resolve_chat_params(
                    context.model_entry or {},
                    context.chat_channel,
                )
            await save_and_execute_recall(
                context,
                assistant_message,
                assistant_key=assistant_key,
                tool_key=tool_key,
                response_id=str(uuid.uuid4()),
                assistant_already_saved=True,
            )
            return build_result(context, "completed")
        if tool_message is not None:
            _log_failure(context, "orphan_tool_dedupe_record")
            return build_result(context, "failed", "orphan_tool_dedupe_record")

        if not await select_initial_channel(context):
            _log_failure(context, "channel_unavailable")
            return build_result(context, "failed", "channel_unavailable")

        base_messages = context.messages
        correction_messages: list[InternalMessage] | None = None
        excluded_priorities: set[int] = set()
        for attempt in range(2):
            working_messages = base_messages if correction_messages is None else correction_messages
            while True:
                try:
                    request_messages, metadata, response_id = await prepare_request_messages(
                        context,
                        working_messages,
                        is_main_context=correction_messages is None,
                    )
                    response = await generate(context, request_messages, metadata)
                    await update_output_metadata(context, response)
                    break
                except LLMException as exc:
                    priority = getattr(context.channel_rule, "priority", None)
                    if isinstance(priority, int) and not isinstance(priority, bool):
                        excluded_priorities.add(priority)
                    logger.bind(
                        uid=context.uid,
                        session_id=context.session_id,
                        priority=priority,
                        call_context=f"chat_dispatch_{context.dispatcher_mode}_memory_recall",
                        **channel_log_extra(context.chat_channel_obj, context.model_entry or {}),
                    ).warning(
                        t(
                            LOG_MEMORY_RECALL_CHANNEL_FAILED,
                            error=format_exception_message(exc),
                        ),
                    )
                    if not await fallback_channel(context, excluded_priorities):
                        _log_failure(context, "llm_exception")
                        return build_result(context, "failed", "llm_exception")
                    if correction_messages is not None:
                        reassemble_multimodal_messages(
                            correction_messages,
                            *get_multimodal_from_entry(context.model_entry),
                        )
            if response_is_valid(response):
                await save_and_execute_recall(
                    context,
                    response.message,
                    assistant_key=assistant_key,
                    tool_key=tool_key,
                    response_id=response_id,
                )
                return build_result(context, "completed")
            if attempt == 0:
                base_messages = context.messages
                correction_messages = build_correction_messages(base_messages, response)

        _log_failure(context, "invalid_recall_response")
        return build_result(context, "failed", "invalid_recall_response")
    except Exception as exc:
        error_type = type(exc).__name__
        _log_failure(context, error_type)
        return build_result(context, "failed", error_type)


memory_recall_precheck = run_memory_recall_precheck


__all__ = [
    "MemoryRecallContext",
    "MemoryRecallPrecheckResult",
    "memory_recall_precheck",
    "run_memory_recall_precheck",
]
