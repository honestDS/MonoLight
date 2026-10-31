from app.core.constants import (
    IMAGE_GENERATION_TOOL_NAME,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)
from app.models.profile import ProfileConfig

_SELF_MANAGED_TIMEOUT_TOOL_NAMES = frozenset(
    {
        SHELL_TOOL_NAME,
        TERMINAL_TOOL_NAME,
        TERMINAL_WATCH_TOOL_NAME,
    }
)


def resolve_tool_execution_timeout(tool_name: str, cfg: ProfileConfig) -> float | None:
    if tool_name in _SELF_MANAGED_TIMEOUT_TOOL_NAMES:
        return None
    tool_config = cfg.tool
    if tool_name == IMAGE_GENERATION_TOOL_NAME:
        return float(tool_config.image_generation_timeout)
    return float(tool_config.tool_timeout)


def resolve_tool_claim_timeout(cfg: ProfileConfig) -> float:
    return float(cfg.tool.tool_timeout)
