import json
from typing import Any

from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_FAILED, MSG_BACKGROUND_TASK_EXECUTION_SUCCEEDED
from app.core.i18n import t
from app.schemas.background_task import BackgroundTaskResult


def normalize_execution_value(
    value: Any,
    active_container_ids: set[int] | None = None,
) -> Any:
    active_ids = active_container_ids if active_container_ids is not None else set()
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return {str(key): normalize_execution_value(item, active_ids) for key, item in value.items()}
        finally:
            active_ids.remove(value_id)
    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in active_ids:
            return "<circular reference>"
        active_ids.add(value_id)
        try:
            return [normalize_execution_value(item, active_ids) for item in value]
        finally:
            active_ids.remove(value_id)
    return str(value)


def serialize_execution_summary(
    value: Any,
    *,
    max_chars: int = 1000,
) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            serialized = value
        else:
            serialized = json.dumps(normalize_execution_value(parsed), ensure_ascii=False)
    else:
        serialized = json.dumps(normalize_execution_value(value), ensure_ascii=False)
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
