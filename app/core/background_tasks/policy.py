from typing import Any

from app.core.constants import (
    IMAGE_GENERATION_TOOL_NAME,
    SHELL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)


def requires_background_execution(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    if tool_name in {IMAGE_GENERATION_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME}:
        return True
    return tool_name == SHELL_TOOL_NAME and arguments is not None and arguments.get("execution_mode") == "non_interactive"


def is_terminal_observer(tool_name: str) -> bool:
    return tool_name == TERMINAL_WATCH_TOOL_NAME
