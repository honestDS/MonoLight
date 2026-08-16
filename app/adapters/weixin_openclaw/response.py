from __future__ import annotations

import json
from typing import Any

from app.core.utils.assistant_files import merge_assistant_files, parse_assistant_files_content


def _parse_audit_confirmation_text(content: object) -> str | None:
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "audit_confirmation":
        return None
    plain_text = payload.get("plain_text")
    return plain_text if isinstance(plain_text, str) and plain_text else None


def extract_reply_text(llm_response: dict[str, Any]) -> str:
    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    audit_text = _parse_audit_confirmation_text(content)
    if audit_text is not None:
        return audit_text
    return parse_assistant_files_content(content)


def extract_reply_files(llm_response: dict[str, Any]) -> list[dict[str, Any]]:
    choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    return merge_assistant_files(
        llm_response.get("files") if isinstance(llm_response, dict) else None,
        message.get("files") if isinstance(message, dict) else None,
    )


def extract_event_reply(event: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    audit_text = _parse_audit_confirmation_text(event.get("content"))
    text = parse_assistant_files_content(event.get("content"))
    if audit_text is not None:
        text = audit_text
    files = merge_assistant_files(event.get("files"))
    return text, files
