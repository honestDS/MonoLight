import copy
import hashlib
import json
from typing import Any

from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K, FILE_TOOL_NAME
from app.core.tools.read_text_file import READ_TEXT_FILE_TOOL_SCHEMA
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.core.utils.tokenizer import estimate_tokens, truncate_text_to_tokens
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import estimate_request_context_tokens

AUDIT_CONTEXT_SAFETY_RATIO_PERCENT = 10
AUDIT_CONTEXT_SAFETY_MIN_TOKENS = 256
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA = copy.deepcopy(READ_TEXT_FILE_TOOL_SCHEMA)
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["description"] = "Read any UTF-8 text file as evidence for one tool call in this audit round."
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tool_call_id"] = {
    "type": "string",
    "description": "The original tool_call_id whose assessment needs this file evidence.",
}
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["parameters"]["required"].append("tool_call_id")
AUDIT_SHELL_TOOL_SCHEMA = copy.deepcopy(SHELL_TOOL_SCHEMA)
AUDIT_SHELL_TOOL_SCHEMA["function"]["description"] = (
    "Use the general-purpose shell to verify environment variables, actual directories, symbolic links, and other environmental facts as audit evidence. "
    "The pending business operation has not been executed. Use only a separate verification command; do not modify files or configuration, and do not execute the pending business command. "
    f"{ShellExecutor.get_blacklist_description()}"
)
AUDIT_SHELL_TOOL_SCHEMA["function"]["parameters"]["properties"]["command"]["description"] = "A separate shell command used only to verify environmental facts; do not provide or execute the pending business command."
AUDIT_SHELL_TOOL_SCHEMA["function"]["parameters"]["properties"]["execution_mode"]["enum"] = ["non_interactive"]
AUDIT_SHELL_TOOL_SCHEMA["function"]["parameters"]["properties"]["execution_mode"]["description"] = "For audit evidence, use only non_interactive and finish within the existing tool_timeout. Do not create terminals or background tasks, switch languages, or wrap the original business command to evade these restrictions."
AUDIT_SHELL_TOOL_SCHEMA["function"]["parameters"]["properties"]["tool_call_id"] = {
    "type": "string",
    "description": "The original tool_call_id of the pending business operation being assessed.",
}
AUDIT_SHELL_TOOL_SCHEMA["function"]["parameters"]["required"].append("tool_call_id")


def max_input_tokens(chat_params: dict[str, Any]) -> int:
    try:
        context_window_k = max(1, int(chat_params["context_window_k"]))
    except (KeyError, TypeError, ValueError):
        context_window_k = 4
    try:
        max_output_tokens = max(0, int(chat_params["max_tokens"]))
    except (KeyError, TypeError, ValueError):
        max_output_tokens = 0
    context_window_tokens = context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
    safety_tokens = max(
        AUDIT_CONTEXT_SAFETY_MIN_TOKENS,
        context_window_tokens * AUDIT_CONTEXT_SAFETY_RATIO_PERCENT // 100,
    )
    return max(
        0,
        context_window_tokens - max_output_tokens - safety_tokens,
    )


def fit_file_tool_payload_to_context(
    system_prompt: str,
    payload: dict[str, Any],
    chat_params: dict[str, Any],
    *,
    model_id: str | None = None,
    protocol: str = "openai",
) -> dict[str, Any]:
    adapted_payload = copy.deepcopy(payload)
    input_limit = max_input_tokens(chat_params)

    def request_context_tokens(candidate_payload: dict[str, Any]) -> int:
        messages = [
            InternalMessage(role=MessageRole.SYSTEM, content=system_prompt),
            InternalMessage(role=MessageRole.USER, content=json.dumps(candidate_payload, ensure_ascii=False)),
        ]
        return estimate_request_context_tokens(
            messages,
            [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA, AUDIT_SHELL_TOOL_SCHEMA],
            model_id=model_id,
            protocol=protocol,
        )

    if request_context_tokens(adapted_payload) <= input_limit:
        return adapted_payload

    tool_calls = adapted_payload.get("tool_calls")
    content_fields: list[tuple[int, str, str, int, str]] = []
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict) or tool_call.get("tool_name", tool_call.get("name")) != FILE_TOOL_NAME:
                continue
            arguments = tool_call.get("arguments")
            if not isinstance(arguments, dict):
                continue
            operation = arguments.get("operation")
            if operation == "write":
                field_names = ("content",)
            elif operation == "edit":
                field_names = ("old_text", "new_text")
            elif operation == "patch":
                field_names = ("patch",)
            elif operation == "grep":
                field_names = ("pattern",)
            else:
                field_names = ()
            for field_name in field_names:
                field_value = arguments.get(field_name)
                if not isinstance(field_value, str):
                    continue
                content_bytes = field_value.encode("utf-8")
                content_fields.append(
                    (
                        index,
                        field_name,
                        field_value,
                        len(content_bytes),
                        hashlib.sha256(content_bytes).hexdigest(),
                    )
                )
    evidence_fields: list[tuple[str, int, str]] = []
    for field_name in ("file_reads", "shell_results"):
        field_items = adapted_payload.get(field_name)
        if not isinstance(field_items, list):
            continue
        for index, item in enumerate(field_items):
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                evidence_fields.append((field_name, index, item["content"]))
    if not content_fields and not evidence_fields:
        return adapted_payload

    def payload_with_content_limit(content_limit: int) -> dict[str, Any]:
        candidate_payload = copy.deepcopy(adapted_payload)
        candidate_calls = candidate_payload.get("tool_calls", [])
        for index, field_name, original_content, original_size, original_sha256 in content_fields:
            content_prefix, truncated = truncate_text_to_tokens(
                original_content,
                content_limit,
                model_id=model_id,
                protocol=protocol,
            )
            tool_call = candidate_calls[index]
            arguments = tool_call["arguments"]
            arguments[field_name] = content_prefix
            existing_evidence = tool_call.get("argument_evidence")
            argument_evidence = dict(existing_evidence) if isinstance(existing_evidence, dict) else {}
            argument_evidence[field_name] = {
                "status": "ok",
                "size": original_size,
                "sha256": original_sha256,
                "truncated": truncated,
                "bytes_read": len(content_prefix.encode("utf-8")),
            }
            tool_call["argument_evidence"] = argument_evidence
        for field_name, index, original_content in evidence_fields:
            content_prefix, truncated = truncate_text_to_tokens(
                original_content,
                content_limit,
                model_id=model_id,
                protocol=protocol,
            )
            item = candidate_payload[field_name][index]
            item["content"] = content_prefix
            item["bytes_read"] = len(content_prefix.encode("utf-8"))
            item["truncated"] = bool(item.get("truncated", False)) or truncated
        return candidate_payload

    empty_payload = payload_with_content_limit(0)
    if request_context_tokens(empty_payload) > input_limit:
        return empty_payload

    low = 0
    all_contents = [content for _, _, content, _, _ in content_fields]
    all_contents.extend(content for _, _, content in evidence_fields)
    high = max(max(estimate_tokens(content, model_id=model_id, protocol=protocol), 1) for content in all_contents)
    fitted_payload = empty_payload
    while low < high:
        candidate_limit = (low + high + 1) // 2
        candidate_payload = payload_with_content_limit(candidate_limit)
        if request_context_tokens(candidate_payload) <= input_limit:
            low = candidate_limit
            fitted_payload = candidate_payload
        else:
            high = candidate_limit - 1
    return fitted_payload


def read_token_budget(
    messages: list[InternalMessage],
    chat_params: dict[str, Any],
    *,
    model_id: str | None = None,
    protocol: str = "openai",
) -> tuple[int, int]:
    request_context_tokens = estimate_request_context_tokens(
        messages,
        [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA, AUDIT_SHELL_TOOL_SCHEMA],
        model_id=model_id,
        protocol=protocol,
    )
    return max(0, max_input_tokens(chat_params) - request_context_tokens), request_context_tokens


def build_read_tool_message(
    tool_call_id: str,
    read_result: dict[str, Any],
) -> InternalMessage:
    return InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call_id,
        content=json.dumps(read_result, ensure_ascii=False),
    )


def fit_read_result_to_context(
    messages: list[InternalMessage],
    tool_call_id: str,
    read_result: dict[str, Any],
    chat_params: dict[str, Any],
    *,
    model_id: str | None = None,
    protocol: str = "openai",
) -> dict[str, Any]:
    content = read_result.get("content")
    if not isinstance(content, str):
        return read_result

    input_limit = max_input_tokens(chat_params)
    tools = [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA, AUDIT_SHELL_TOOL_SCHEMA]
    if (
        estimate_request_context_tokens(
            [*messages, build_read_tool_message(tool_call_id, read_result)],
            tools,
            model_id=model_id,
            protocol=protocol,
        )
        <= input_limit
    ):
        return read_result

    low = 0
    high = max(estimate_tokens(content, model_id=model_id, protocol=protocol), 1)
    fitted_content = ""
    while low < high:
        candidate_tokens = (low + high + 1) // 2
        candidate_content, _ = truncate_text_to_tokens(
            content,
            candidate_tokens,
            model_id=model_id,
            protocol=protocol,
        )
        candidate_result = {
            **read_result,
            "content": candidate_content,
            "bytes_read": len(candidate_content.encode("utf-8")),
            "truncated": True,
        }
        candidate_context_tokens = estimate_request_context_tokens(
            [*messages, build_read_tool_message(tool_call_id, candidate_result)],
            tools,
            model_id=model_id,
            protocol=protocol,
        )
        if candidate_context_tokens <= input_limit:
            low = candidate_tokens
            fitted_content = candidate_content
        else:
            high = candidate_tokens - 1
    return {
        **read_result,
        "content": fitted_content,
        "bytes_read": len(fitted_content.encode("utf-8")),
        "truncated": True,
    }
