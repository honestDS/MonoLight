import os
from pathlib import Path
from typing import Any

from app.core.constants import WRITE_PROTECTED_DIRECTORIES


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


def get_write_protected_dirs(project_root: str | Path) -> list[Path]:
    root_path = Path(os.path.abspath(Path(project_root)))
    protected_dirs: list[Path] = []
    for directory in WRITE_PROTECTED_DIRECTORIES:
        original_path = root_path / directory
        lexical_path = Path(os.path.abspath(original_path))
        for path in (lexical_path, original_path.resolve(strict=False)):
            if path not in protected_dirs:
                protected_dirs.append(path)
    return protected_dirs


def is_path_write_protected(path: str | Path, project_root: str | Path) -> bool:
    root_path = Path(os.path.abspath(Path(project_root)))
    candidate_path = Path(path)
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    lexical_path = Path(os.path.abspath(candidate_path))
    candidate_paths = (lexical_path, candidate_path.resolve(strict=False))
    protected_dirs = get_write_protected_dirs(root_path)

    for candidate in candidate_paths:
        for protected_dir in protected_dirs:
            if candidate.is_relative_to(protected_dir) or protected_dir.is_relative_to(candidate):
                return True
    return False
