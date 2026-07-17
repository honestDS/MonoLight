import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_AUDIT_FILE_NAME_INVALID,
    ERR_AUDIT_FILE_OUTSIDE_USER_DIR,
    ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE,
    ERR_AUDIT_FILE_RECORD_MISMATCH,
    ERR_AUDIT_FILE_SYMLINK,
    ERR_AUDIT_RETENTION_INVALID,
    ERR_AUDIT_ROOT_SYMLINK,
    ERR_AUDIT_USER_DIR_OUTSIDE,
    ERR_AUDIT_USER_DIR_SYMLINK,
)
from app.core.i18n import t
from app.core.paths import AUDIT_DIR, AUDIT_FILE_PREFIX, AUDIT_FILE_SUFFIX, USER_TEMP_DIR_PREFIX, get_audit_file_path, get_user_audit_dir


@dataclass(slots=True)
class AuditCleanupResult:
    deleted_files: list[str] = field(default_factory=list)
    missing_referenced_files: list[str] = field(default_factory=list)
    failed_paths: dict[str, str] = field(default_factory=dict)


def _ensure_audit_root(audit_root: str | Path) -> Path:
    root_path = Path(audit_root)
    if root_path.is_symlink():
        raise ValueError(t(ERR_AUDIT_ROOT_SYMLINK))
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path.resolve(strict=True)


def _ensure_user_audit_dir(uid: str, audit_root: str | Path) -> Path:
    root_path = _ensure_audit_root(audit_root)
    user_path = get_user_audit_dir(uid, audit_root=root_path)
    if user_path.is_symlink():
        raise ValueError(t(ERR_AUDIT_USER_DIR_SYMLINK))
    user_path.mkdir(parents=False, exist_ok=True)
    resolved_user_path = user_path.resolve(strict=True)
    if resolved_user_path.parent != root_path:
        raise ValueError(t(ERR_AUDIT_USER_DIR_OUTSIDE))
    return resolved_user_path


def validate_audit_file_path(
    path: str | Path,
    *,
    uid: str,
    audit_record_id: int | None = None,
    audit_root: str | Path = AUDIT_DIR,
    require_exists: bool = False,
) -> Path:
    root_path = Path(audit_root).resolve(strict=False)
    user_path = get_user_audit_dir(uid, audit_root=root_path)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(t(ERR_AUDIT_FILE_PATH_NOT_ABSOLUTE))
    if candidate.is_symlink():
        raise ValueError(t(ERR_AUDIT_FILE_SYMLINK))

    resolved_candidate = candidate.resolve(strict=require_exists)
    if resolved_candidate.parent != user_path:
        raise ValueError(t(ERR_AUDIT_FILE_OUTSIDE_USER_DIR))
    if audit_record_id is not None and resolved_candidate != get_audit_file_path(uid, audit_record_id, audit_root=root_path):
        raise ValueError(t(ERR_AUDIT_FILE_RECORD_MISMATCH))
    record_id_text = resolved_candidate.name[len(AUDIT_FILE_PREFIX) : -len(AUDIT_FILE_SUFFIX)]
    if resolved_candidate.suffix != AUDIT_FILE_SUFFIX or not record_id_text.isdigit() or int(record_id_text) < 1:
        raise ValueError(t(ERR_AUDIT_FILE_NAME_INVALID))
    return resolved_candidate


def _write_audit_json_sync(
    uid: str,
    audit_record_id: int,
    payload: dict[str, Any],
    audit_root: str | Path,
) -> Path:
    user_path = _ensure_user_audit_dir(uid, audit_root)
    target_path = get_audit_file_path(uid, audit_record_id, audit_root=audit_root)
    target_path = validate_audit_file_path(target_path.absolute(), uid=uid, audit_record_id=audit_record_id, audit_root=audit_root)
    if target_path.is_symlink():
        raise ValueError(t(ERR_AUDIT_FILE_SYMLINK))

    serialized = canonical_json_dumps(payload).encode("utf-8")
    temp_path = user_path / f".{target_path.stem}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    file_descriptor = None
    try:
        file_descriptor = os.open(temp_path, flags, 0o600)
        with os.fdopen(file_descriptor, "wb") as file_handle:
            file_descriptor = None
            file_handle.write(serialized)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        if temp_path.is_symlink() or target_path.is_symlink():
            raise ValueError(t(ERR_AUDIT_FILE_SYMLINK))
        os.replace(temp_path, target_path)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    return validate_audit_file_path(target_path, uid=uid, audit_record_id=audit_record_id, audit_root=audit_root, require_exists=True)


async def write_audit_json(
    *,
    uid: str,
    audit_record_id: int,
    payload: dict[str, Any],
    audit_root: str | Path = AUDIT_DIR,
) -> Path:
    return await asyncio.to_thread(_write_audit_json_sync, uid, audit_record_id, payload, audit_root)


async def write_audit_json_and_associate(
    *,
    uid: str,
    audit_record_id: int,
    payload: dict[str, Any],
    associate_path: Callable[[str], Awaitable[None]],
    audit_root: str | Path = AUDIT_DIR,
) -> Path:
    stored_path = await write_audit_json(uid=uid, audit_record_id=audit_record_id, payload=payload, audit_root=audit_root)
    validated_path = validate_audit_file_path(stored_path, uid=uid, audit_record_id=audit_record_id, audit_root=audit_root, require_exists=True)
    await associate_path(str(validated_path))
    return validated_path


def cleanup_audit_storage(
    *,
    retention_days: int,
    audit_root: str | Path = AUDIT_DIR,
    referenced_paths: set[str] | None = None,
    now_timestamp: float | None = None,
) -> AuditCleanupResult:
    if retention_days < 1:
        raise ValueError(t(ERR_AUDIT_RETENTION_INVALID))

    result = AuditCleanupResult()
    root_path = _ensure_audit_root(audit_root)
    cutoff = (now_timestamp if now_timestamp is not None else time.time()) - retention_days * 86400
    normalized_references = {str(Path(path).resolve(strict=False)) for path in referenced_paths or set()}
    existing_files: set[str] = set()

    with os.scandir(root_path) as user_entries:
        for user_entry in user_entries:
            if not user_entry.is_dir(follow_symlinks=False) or not user_entry.name.startswith(USER_TEMP_DIR_PREFIX):
                continue
            user_path = Path(user_entry.path)
            try:
                with os.scandir(user_path) as file_entries:
                    for file_entry in file_entries:
                        file_path = Path(file_entry.path)
                        if file_entry.is_symlink():
                            should_delete = True
                        elif not file_entry.is_file(follow_symlinks=False):
                            continue
                        else:
                            existing_files.add(str(file_path.resolve(strict=False)))
                            is_audit_json = file_entry.name.startswith(AUDIT_FILE_PREFIX) and file_entry.name.endswith(AUDIT_FILE_SUFFIX)
                            is_temp_file = file_entry.name.startswith(f".{AUDIT_FILE_PREFIX}") and file_entry.name.endswith(".tmp")
                            if not is_audit_json and not is_temp_file:
                                continue
                            stat_result = file_entry.stat(follow_symlinks=False)
                            is_expired = stat_result.st_mtime < cutoff
                            is_orphan = referenced_paths is not None and str(file_path.resolve(strict=False)) not in normalized_references
                            should_delete = is_expired or is_orphan

                        if not should_delete:
                            continue
                        try:
                            file_path.unlink()
                            result.deleted_files.append(str(file_path.resolve(strict=False)))
                        except OSError as exc:
                            result.failed_paths[str(file_path)] = str(exc)
            except OSError as exc:
                result.failed_paths[str(user_path)] = str(exc)

    if referenced_paths is not None:
        result.missing_referenced_files = sorted(normalized_references - existing_files)
    return result
