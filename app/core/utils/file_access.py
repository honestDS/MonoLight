import asyncio
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.constants import (
    ERR_FILE_ARGUMENT_INVALID,
    ERR_FILE_NOT_FOUND,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_SESSION_NO_PERMISSION,
)
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.paths import TEMP_DIR, USER_TEMP_DIR_PREFIX
from app.core.tools.send_file_to_user import _is_sensitive_path


def _is_safe_directory_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and value not in {".", ".."} and PurePosixPath(value).name == value and PureWindowsPath(value).name == value


def _normalize_allowed_root(normalized_temp_root: Path, directory_name: str) -> Path:
    allowed_root = normalized_temp_root / directory_name
    try:
        normalized_allowed_root = allowed_root.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID) from exc
    if normalized_allowed_root.parent != normalized_temp_root or normalized_allowed_root.name != directory_name:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
    return normalized_allowed_root


def _resolve_user_temp_file_sync(
    path: str,
    *,
    uid: str,
    temp_root: Path,
) -> Path:
    if not _is_safe_directory_name(uid):
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID)
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID)

    try:
        normalized_temp_root = Path(temp_root).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID) from exc

    normalized_user_root = _normalize_allowed_root(normalized_temp_root, f"{USER_TEMP_DIR_PREFIX}{uid}")
    try:
        normalized_path = Path(path).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID) from exc

    if not _is_within_root(normalized_path, normalized_user_root):
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
    if _is_sensitive_path(normalized_path):
        raise ForbiddenException(ERR_FILE_SENSITIVE_NOT_ALLOWED)
    if not normalized_path.exists() or not normalized_path.is_file():
        raise ResourceNotFoundException(ERR_FILE_NOT_FOUND)
    return normalized_path


def _is_within_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


async def resolve_user_temp_file(
    path: str,
    *,
    uid: str,
    temp_root: Path | None = None,
) -> Path:
    temp_root = TEMP_DIR if temp_root is None else temp_root
    return await asyncio.to_thread(
        _resolve_user_temp_file_sync,
        path,
        uid=uid,
        temp_root=temp_root,
    )


async def validate_web_message_files(
    message: str | list[object] | None,
    attachments: list[str] | None,
    *,
    uid: str,
    temp_root: Path | None = None,
) -> None:
    paths: list[object] = []
    for attachment in attachments or []:
        if attachment not in paths:
            paths.append(attachment)
    if isinstance(message, list):
        for part in message:
            if isinstance(part, dict):
                part_type = part.get("type")
                path = part.get("path")
            else:
                part_type = getattr(part, "type", None)
                path = getattr(part, "path", None)
            if part_type in {"file", "audio"} and path is not None and path not in paths:
                paths.append(path)

    if not paths:
        return

    temp_root = TEMP_DIR if temp_root is None else temp_root
    for path in paths:
        await resolve_user_temp_file(path, uid=uid, temp_root=temp_root)
