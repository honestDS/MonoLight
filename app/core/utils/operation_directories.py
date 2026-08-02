from pathlib import Path
from typing import Any


def get_allowed_operation_dirs(config: Any | None) -> list[str]:
    """Read configured operation directories from a profile or tool config."""
    tool_config = getattr(config, "tool", config) if config is not None else None
    allowed_dirs = getattr(tool_config, "allowed_operation_dirs", []) if tool_config is not None else []
    if isinstance(allowed_dirs, list):
        return [directory for directory in allowed_dirs if isinstance(directory, str)]
    return []


def normalize_allowed_operation_dirs(allowed_dirs: list[str] | None = None) -> list[Path]:
    normalized_dirs: list[Path] = []
    for directory in allowed_dirs or []:
        try:
            directory_path = Path(directory)
            if directory_path.is_absolute():
                normalized_dirs.append(directory_path.resolve(strict=False))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return normalized_dirs


def is_path_within_allowed_operation_dirs(path: str | Path, allowed_dirs: list[str] | None = None) -> bool:
    try:
        candidate_path = Path(path)
        if not candidate_path.is_absolute():
            return False
        resolved_path = candidate_path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False

    for root in normalize_allowed_operation_dirs(allowed_dirs):
        try:
            resolved_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
