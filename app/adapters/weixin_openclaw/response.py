from __future__ import annotations

from typing import Any

from app.core.utils.assistant_files import merge_assistant_files, parse_assistant_files_content


def extract_reply_text(llm_response: dict[str, Any]) -> str:
    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    text, _files = parse_assistant_files_content(content)
    return text


def extract_reply_files(llm_response: dict[str, Any]) -> list[dict[str, Any]]:
    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    _text, content_files = parse_assistant_files_content(content)
    return merge_assistant_files(
        llm_response.get("files") if isinstance(llm_response, dict) else None,
        message.get("files") if isinstance(message, dict) else None,
        content_files,
    )


def extract_event_reply(event: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text, content_files = parse_assistant_files_content(event.get("content"))
    history_file_groups: list[list[dict[str, Any]]] = []
    for item in event.get("history") or []:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        history_text, history_files = parse_assistant_files_content(item.get("content"))
        if history_files:
            if not text:
                text = history_text
            history_file_groups.append(history_files)
    files = merge_assistant_files(event.get("files"), content_files, *history_file_groups)
    return text, files
