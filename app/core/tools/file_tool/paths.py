from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.constants import ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS
from app.core.i18n import t
from app.core.utils.operation_directories import normalize_allowed_operation_dirs


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_file_tool_target_path(
    path: str,
    workspace_path: str | Path,
    allowed_operation_dirs: list[str] | None = None,
) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))

    workspace = Path(workspace_path).resolve(strict=False)
    candidate = Path(path)
    portable_absolute = PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
    if portable_absolute and not candidate.is_absolute():
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))

    target = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace / candidate).resolve(strict=False)
    roots = [workspace, *normalize_allowed_operation_dirs(allowed_operation_dirs)]
    if not any(is_within(target, root) for root in roots):
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))
    return target
