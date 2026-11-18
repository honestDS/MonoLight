import asyncio
import os
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    AUDIT_ORPHAN_FILE_GRACE_SECONDS,
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
from app.core.crud.account.file_download_cleanup import FileDownloadCleanupJournal
from app.core.i18n import t
from app.core.paths import AUDIT_DIR, AUDIT_FILE_PREFIX, AUDIT_FILE_SUFFIX, USER_TEMP_DIR_PREFIX, get_audit_file_path, get_user_audit_dir


@dataclass(frozen=True, slots=True)
class AuditFileCleanupCandidate:
    path: Path
    file_stat: os.stat_result | None
    referenced: bool


@dataclass(slots=True)
class AuditStorageCleanupPlan:
    candidates: list[AuditFileCleanupCandidate] = field(default_factory=list)
    failed_paths: dict[str, str] = field(default_factory=dict)


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


def plan_audit_storage_cleanup(
    *,
    retention_days: int,
    audit_root: str | Path = AUDIT_DIR,
    referenced_paths: set[str] | None = None,
    protected_paths: set[str] | None = None,
    now_timestamp: float | None = None,
) -> AuditStorageCleanupPlan:
    if retention_days < 1:
        raise ValueError(t(ERR_AUDIT_RETENTION_INVALID))

    plan = AuditStorageCleanupPlan()
    root_path = Path(audit_root).absolute()

    try:
        root_path.lstat()
    except FileNotFoundError:
        return plan
    except OSError as exc:
        plan.failed_paths[str(root_path)] = str(exc)
        return plan
    if root_path.is_symlink() or root_path.is_junction():
        raise ValueError(t(ERR_AUDIT_ROOT_SYMLINK))
    root_path = root_path.resolve(strict=True)

    current_timestamp = now_timestamp if now_timestamp is not None else time.time()
    cutoff = current_timestamp - retention_days * 86400
    orphan_cutoff = current_timestamp - AUDIT_ORPHAN_FILE_GRACE_SECONDS
    normalized_references = {Path(path).absolute() for path in referenced_paths or set()}
    normalized_protected = {Path(path).absolute() for path in protected_paths or set()}
    existing_files: set[Path] = set()
    unavailable_user_paths: set[Path] = set()
    failed_user_paths: set[Path] = set()
    root_scan_failed = False

    def _is_local_audit_reference(path: Path) -> bool:
        try:
            relative_path = path.relative_to(root_path)
        except ValueError:
            return False
        if len(relative_path.parts) != 2:
            return False
        user_name, file_name = relative_path.parts
        if not user_name.startswith(USER_TEMP_DIR_PREFIX) or len(user_name) == len(USER_TEMP_DIR_PREFIX):
            return False
        if not file_name.startswith(AUDIT_FILE_PREFIX) or not file_name.endswith(AUDIT_FILE_SUFFIX):
            return False
        record_id = file_name[len(AUDIT_FILE_PREFIX) : -len(AUDIT_FILE_SUFFIX)]
        return record_id.isascii() and record_id.isdecimal() and record_id.lstrip("0") != ""

    def _is_protected_file(file_path: Path, is_temp_file: bool) -> bool:
        if file_path in normalized_protected:
            return True
        if not is_temp_file:
            return False
        temp_basename = file_path.name[1:].split(".")[0]
        protected_target = file_path.with_name(f"{temp_basename}{AUDIT_FILE_SUFFIX}")
        return protected_target in normalized_protected

    try:
        with os.scandir(root_path) as user_entries:
            for user_entry in user_entries:
                if not user_entry.name.startswith(USER_TEMP_DIR_PREFIX):
                    continue
                user_path = Path(user_entry.path).absolute()
                if user_entry.is_symlink() or user_entry.is_junction():
                    unavailable_user_paths.add(user_path)
                    continue
                if not user_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(user_path) as file_entries:
                        for file_entry in file_entries:
                            file_path = Path(file_entry.path).absolute()
                            is_audit_json = file_entry.name.startswith(AUDIT_FILE_PREFIX) and file_entry.name.endswith(AUDIT_FILE_SUFFIX)
                            is_temp_file = file_entry.name.startswith(f".{AUDIT_FILE_PREFIX}") and file_entry.name.endswith(".tmp")
                            if not is_audit_json and not is_temp_file:
                                continue

                            if is_audit_json and file_path in normalized_references:
                                existing_files.add(file_path)
                            if _is_protected_file(file_path, is_temp_file):
                                continue
                            try:
                                file_stat = file_path.lstat()
                            except OSError as exc:
                                plan.failed_paths[str(file_path)] = str(exc)
                                continue

                            is_symlink = stat.S_ISLNK(file_stat.st_mode)
                            if not is_symlink and not stat.S_ISREG(file_stat.st_mode):
                                continue
                            should_plan = is_symlink
                            if not should_plan:
                                is_expired = file_stat.st_mtime < cutoff
                                is_orphan = referenced_paths is not None and file_path not in normalized_references and file_stat.st_mtime < orphan_cutoff
                                should_plan = is_expired or is_orphan
                            if not should_plan:
                                continue
                            plan.candidates.append(
                                AuditFileCleanupCandidate(
                                    path=file_path,
                                    file_stat=file_stat,
                                    referenced=file_path in normalized_references,
                                )
                            )
                except OSError as exc:
                    failed_user_paths.add(user_path)
                    plan.failed_paths[str(user_path)] = str(exc)
    except OSError as exc:
        root_scan_failed = True
        plan.failed_paths[str(root_path)] = str(exc)

    if referenced_paths is None or root_scan_failed:
        return plan

    for reference_path in normalized_references:
        if reference_path in normalized_protected or not _is_local_audit_reference(reference_path):
            continue
        if reference_path in existing_files or reference_path.parent in unavailable_user_paths or reference_path.parent in failed_user_paths:
            continue
        try:
            reference_path.lstat()
        except FileNotFoundError:
            plan.candidates.append(
                AuditFileCleanupCandidate(
                    path=reference_path,
                    file_stat=None,
                    referenced=True,
                )
            )
        except OSError as exc:
            plan.failed_paths[str(reference_path)] = str(exc)
    return plan


def cleanup_audit_storage(
    candidates: list[AuditFileCleanupCandidate],
    *,
    removal_journal: FileDownloadCleanupJournal,
) -> AuditCleanupResult:
    result = AuditCleanupResult()
    missing_referenced_files: set[str] = set()

    def _record_missing(candidate: AuditFileCleanupCandidate, cutoff_ns: int) -> None:
        if not candidate.referenced:
            return
        try:
            removal_journal.note_missing_file(candidate.path, cutoff_ns)
        except OSError as exc:
            result.failed_paths[str(candidate.path)] = str(exc)
            return
        missing_referenced_files.add(str(candidate.path))

    def _has_same_stat(current_stat: os.stat_result, expected_stat: os.stat_result) -> bool:
        return os.path.samestat(current_stat, expected_stat) and current_stat.st_mtime_ns == expected_stat.st_mtime_ns and current_stat.st_ctime_ns == expected_stat.st_ctime_ns and current_stat.st_size == expected_stat.st_size and current_stat.st_mode == expected_stat.st_mode

    for candidate in candidates:
        parent_path = candidate.path.parent
        if parent_path.is_symlink() or parent_path.is_junction():
            continue
        lstat_cutoff_ns = time.time_ns()
        try:
            current_stat = candidate.path.lstat()
        except FileNotFoundError:
            _record_missing(candidate, lstat_cutoff_ns)
            continue
        except OSError as exc:
            result.failed_paths[str(candidate.path)] = str(exc)
            continue

        if candidate.file_stat is None:
            continue
        if not _has_same_stat(current_stat, candidate.file_stat):
            continue
        unlink_cutoff_ns = time.time_ns()
        try:
            removal_journal.unlink_file(candidate.path, expected_stat=candidate.file_stat)
        except FileNotFoundError:
            _record_missing(candidate, unlink_cutoff_ns)
        except OSError as exc:
            result.failed_paths[str(candidate.path)] = str(exc)
        else:
            result.deleted_files.append(str(candidate.path))

    result.missing_referenced_files = sorted(missing_referenced_files)
    return result
