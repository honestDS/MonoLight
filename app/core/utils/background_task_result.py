from typing import Any

from app.core import constants
from app.core.i18n import t
from app.schemas.background_task import BackgroundTaskResult


def build_background_task_success_result(tool_name: str, content: Any) -> dict[str, Any]:
    return BackgroundTaskResult(
        status="succeeded",
        tool_name=tool_name,
        summary=t(constants.MSG_BACKGROUND_TASK_EXECUTION_SUCCEEDED),
        content=content,
    ).model_dump()


def build_background_task_failure_result(tool_name: str, error: str) -> dict[str, Any]:
    return BackgroundTaskResult(
        status="failed",
        tool_name=tool_name,
        summary=t(constants.ERR_BACKGROUND_TASK_EXECUTION_FAILED),
        error=error,
    ).model_dump()
