import json
from importlib import import_module
from typing import Any

from app.core.constants import ERR_INTERNAL_SERVER_ERROR, ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL
from app.core.exceptions import BaseBusinessException, LLMException, ServerException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.tools import TOOL_EXECUTOR_MAP
from app.core.utils.background_task_result import sanitize_execution_summary
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.providers.database import AsyncSessionLocal

BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES = {"send_file_to_user", "query_knowledge_base"}

logger = get_logger(__name__)


def get_multimodal_from_entry(model_entry: dict) -> tuple[bool, bool, bool]:
    return (
        model_entry.get("image_understanding", False),
        model_entry.get("audio_understanding", False),
        model_entry.get("video_understanding", False),
    )


def resolve_chat_params(model_entry: dict, chat_channel) -> dict:
    return {
        "temperature": model_entry.get("temperature") if model_entry.get("temperature") is not None else 0.7,
        "top_p": model_entry.get("top_p"),
        "max_tokens": model_entry.get("max_tokens") if model_entry.get("max_tokens") is not None else 2048,
        "chat_timeout": chat_channel.chat_timeout,
        "context_window_k": model_entry.get("context_window_k") if model_entry.get("context_window_k") is not None else 4,
    }


def format_exception_message(exc: Exception) -> str:
    if isinstance(exc, BaseBusinessException):
        return exc.render_message()
    logger.bind(
        exception_type=type(exc).__name__,
        exception_message=sanitize_execution_summary(str(exc)),
    ).opt(exception=exc).error(t("LOG_DISPATCHER_UNKNOWN_EXCEPTION"))
    return t(ERR_INTERNAL_SERVER_ERROR)


def extract_files_to_user(tool_responses: list[InternalMessage]) -> list[dict[str, Any]]:
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


async def process_single_tool_with_isolated_db(
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
    allow_background_submission: bool = True,
) -> InternalMessage:
    dispatcher_module = import_module("app.core.dispatcher")
    async_session_local = getattr(dispatcher_module, "AsyncSessionLocal", AsyncSessionLocal)
    process_tool = getattr(dispatcher_module, "process_single_tool")
    async with async_session_local() as tool_db:
        return await process_tool(
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
            allow_background_submission=allow_background_submission,
        )


def dump_output_history(
    messages: list[InternalMessage],
    *,
    expose_tool_call_content: bool = True,
) -> list[dict[str, Any]]:
    output_messages = messages
    if not expose_tool_call_content:
        output_messages = [message.model_copy(update={"content": None}) if message.role == MessageRole.ASSISTANT and message.tool_calls else message for message in messages]
    return [message.model_dump(exclude_none=True) for message in output_messages]


def dump_background_proactive_history(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in _filter_tool_output_messages(messages)]


def _get_tool_schema_name(schema: dict[str, Any]) -> str | None:
    name = schema.get("function", {}).get("name")
    return name if isinstance(name, str) else None


def filter_background_proactive_tools(tools: list[dict[str, Any]], allowed_tool_names: set[str] | None = None) -> list[dict[str, Any]]:
    allowed_names = allowed_tool_names or BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES
    missing_tool_names = sorted(tool_name for tool_name in allowed_names if tool_name not in TOOL_EXECUTOR_MAP)
    if missing_tool_names:
        raise ServerException(message=ERR_INTERNAL_SERVER_ERROR, missing_tools=", ".join(missing_tool_names))
    return [tool for tool in tools if _get_tool_schema_name(tool) in allowed_names]


def get_unsupported_background_proactive_tool_names(tool_calls: list[InternalToolCall], allowed_tool_names: set[str] | None = None) -> list[str]:
    allowed_names = allowed_tool_names or BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES
    return sorted({tool_call.name for tool_call in tool_calls if tool_call.name not in allowed_names or tool_call.name not in TOOL_EXECUTOR_MAP})


def validate_background_proactive_tool_calls(tool_calls: list[InternalToolCall], allowed_tool_names: set[str] | None = None) -> None:
    unsupported_tool_names = get_unsupported_background_proactive_tool_names(tool_calls, allowed_tool_names=allowed_tool_names)
    if unsupported_tool_names:
        raise LLMException(message=ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL, detail=", ".join(unsupported_tool_names))


def reassemble_multimodal_messages(
    messages: list[InternalMessage],
    image_understanding: bool,
    audio_understanding: bool,
    video_understanding: bool,
) -> None:
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
