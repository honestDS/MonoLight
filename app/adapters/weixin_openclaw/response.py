from __future__ import annotations

import json
from typing import Any


def parse_assistant_files_content(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(content, str):
        return str(content or "").strip(), []
    try:
        parsed = json.loads(content)
    except Exception:
        return content.strip(), []
    if not isinstance(parsed, dict) or parsed.get("type") != "assistant_files":
        return content.strip(), []
    text = str(parsed.get("text") or "").strip()
    files = [item for item in parsed.get("files") or [] if isinstance(item, dict)]
    return text, files


def extract_reply_text(llm_response: dict[str, Any]) -> str:
    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    text, _files = parse_assistant_files_content(content)
    return text


def extract_reply_files(llm_response: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_file_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or item.get("path") or "").strip()
            if file_id and file_id in seen_ids:
                continue
            if file_id:
                seen_ids.add(file_id)
            files.append(item)

    if isinstance(llm_response, dict):
        add_file_items(llm_response.get("files"))

    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    if choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        _text, content_files = parse_assistant_files_content(content)
        add_file_items(content_files)

    return files


def extract_event_reply(event: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text, files = parse_assistant_files_content(event.get("content"))
    if not files:
        for item in event.get("history") or []:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role != "assistant" or content is None:
                continue
            history_text, history_files = parse_assistant_files_content(content)
            if history_files:
                if not text:
                    text = history_text
                files.extend(history_files)
    return text, files
