import json
import re
from typing import Any

from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_FAILED, MSG_BACKGROUND_TASK_EXECUTION_SUCCEEDED
from app.core.i18n import t
from app.schemas.background_task import BackgroundTaskResult

_SENSITIVE_KEY_PATTERN = re.compile(r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|token|private[_-]?key|b64[_-]?json)", re.IGNORECASE)
_OUTPUT_KEY_PATTERN = re.compile(r"^(?:stdout|stderr|output|raw[_-]?output|shell[_-]?output|command[_-]?output|tool[_-]?output|content|result)$", re.IGNORECASE)
_SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(\b(?:api[_-]?key|authorization|cookie|password|passwd|secret|token)\s*[:=]\s*)[^\s,;]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(\s--?(?:api[_-]?key|authorization|cookie|password|passwd|secret|token)(?:=|\s+)\s*)[^\s,;]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(\b(?:OPENAI|AWS|AZURE|FIRECRAWL|MONOLIGH)[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\s*=\s*)[^\s,;]+", re.IGNORECASE), r"\1<redacted>"),
)


def sanitize_execution_value(
    value: Any,
    active_container_ids: set[int] | None = None,
    *,
    redact_output: bool = False,
    redact_sensitive: bool = True,
) -> Any:
    active_ids = active_container_ids if active_container_ids is not None else set()
    if isinstance(value, str):
        sanitized = value
        if redact_sensitive:
            for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
                sanitized = pattern.sub(replacement, sanitized)
        return sanitized
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return {str(key): "<redacted>" if (redact_sensitive and _SENSITIVE_KEY_PATTERN.search(str(key))) or (redact_output and _OUTPUT_KEY_PATTERN.fullmatch(str(key))) else sanitize_execution_value(item, active_ids, redact_output=redact_output, redact_sensitive=redact_sensitive) for key, item in value.items()}
        finally:
            active_ids.remove(value_id)
    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return [sanitize_execution_value(item, active_ids, redact_output=redact_output, redact_sensitive=redact_sensitive) for item in value]
        finally:
            active_ids.remove(value_id)
    return sanitize_execution_value(str(value), active_ids, redact_output=redact_output, redact_sensitive=redact_sensitive)


def sanitize_execution_summary(
    value: Any,
    *,
    max_chars: int = 1000,
    redact_text: bool = False,
    redact_output: bool = True,
    redact_sensitive: bool = True,
) -> str:
    sanitized = sanitize_execution_value(value, redact_output=redact_output, redact_sensitive=redact_sensitive)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            serialized = json.dumps({"redacted": True, "original_chars": len(sanitized)}, ensure_ascii=False) if redact_text else sanitized
        else:
            if redact_text and not isinstance(parsed, (dict, list)):
                serialized = json.dumps({"redacted": True, "original_chars": len(sanitized)}, ensure_ascii=False)
            else:
                serialized = json.dumps(
                    sanitize_execution_value(parsed, redact_output=redact_output, redact_sensitive=redact_sensitive),
                    ensure_ascii=False,
                )
    else:
        serialized = json.dumps(sanitized, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized
    return json.dumps(
        {
            "truncated": True,
            "original_chars": len(serialized),
            "summary": serialized[: max(0, max_chars - 80)],
        },
        ensure_ascii=False,
    )[:max_chars]


def build_background_task_success_result(tool_name: str, content: Any) -> dict[str, Any]:
    return BackgroundTaskResult(
        status="succeeded",
        tool_name=tool_name,
        summary=t(MSG_BACKGROUND_TASK_EXECUTION_SUCCEEDED),
        content=content,
    ).model_dump()


def build_background_task_failure_result(tool_name: str, error: str) -> dict[str, Any]:
    return BackgroundTaskResult(
        status="failed",
        tool_name=tool_name,
        summary=t(ERR_BACKGROUND_TASK_EXECUTION_FAILED),
        error=error,
    ).model_dump()
