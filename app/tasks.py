import asyncio
import errno
import heapq
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from stat import S_ISREG
from time import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.storage import (
    AuditCleanupResult,
    AuditFileCleanupCandidate,
    cleanup_audit_storage,
    plan_audit_storage_cleanup,
)
from app.core.constants import (
    AUDIT_HISTORY_CLEANUP_BATCH_SIZE,
    AUDIT_HISTORY_CLEANUP_INTERVAL_SECONDS,
    ERR_LOG_CLEANER_FAILED,
    ERR_TEMP_CLEANER_DELETE_FAILED,
    ERR_TEMP_CLEANER_FAILED,
    FILE_DOWNLOAD_TOKEN_RECONCILE_GRACE_SECONDS,
    FILE_DOWNLOAD_TOKEN_RECONCILE_INTERVAL_SECONDS,
    MSG_TEMP_CLEANER_CLEARED,
    SYSTEM_LOG_CLEANUP_BATCH_PAUSE_SECONDS,
    SYSTEM_LOG_CLEANUP_BATCH_SIZE,
    SYSTEM_LOG_CLEANUP_INTERVAL_SECONDS,
    SYSTEM_LOG_REALTIME_GRACE_SECONDS,
)
from app.core.crud.account.file_download_cleanup import FileDownloadCleanupJournal
from app.core.crud.account.user import user_crud
from app.core.crud.audit.audit import audit_crud
from app.core.crud.audit.recovery import AuditHistoryCleanupRecord
from app.core.crud.system.log import system_log_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.i18n import t
from app.core.log import get_logger, log_file_rotation_lock
from app.core.paths import AUDIT_DIR, DEFAULT_LOG_FILE_PATH, TEMP_DIR, TOOLS_LOG_FILENAME, get_audit_file_path
from app.core.tools.send_file_to_user import cleanup_file_download_tokens, get_file_download_cleanup_journal
from app.core.user.file_cleanup import recover_user_file_cleanup
from app.core.utils.time import get_local_time
from app.models.audit import AUDIT_TERMINAL_STATUSES, AuditRecordStatus
from app.providers.database import AsyncSessionLocal, ensure_sqlite_outer_transaction

logger = get_logger(__name__)


@dataclass(slots=True)
class AuditHistoryCleanupResult:
    deleted_database_records: int
    file_cleanup: AuditCleanupResult


@dataclass(frozen=True, slots=True)
class _AuditHistoryCleanupWork:
    candidate: AuditFileCleanupCandidate | None
    records: tuple[AuditHistoryCleanupRecord, ...]


def _audit_history_record_completed_before(
    record: AuditHistoryCleanupRecord,
    completed_before: datetime,
) -> bool:
    completed_at = record.completed_at
    if completed_at is None:
        return False
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=completed_before.tzinfo)
    return completed_at < completed_before


def _audit_history_records_completed_before(
    records: tuple[AuditHistoryCleanupRecord, ...],
    completed_before: datetime,
) -> bool:
    return bool(records) and all(_audit_history_record_completed_before(record, completed_before) for record in records)


def _collect_temp_dir_files(temp_dir: Path) -> tuple[int, list[tuple[float, int, os.stat_result, Path]], list[Path]]:
    total_size = 0
    file_heap: list[tuple[float, int, os.stat_result, Path]] = []
    pending_empty_dirs: list[Path] = []
    scan_stack = [temp_dir.absolute()]
    heap_index = 0

    while scan_stack:
        current_dir = scan_stack.pop()
        try:
            if current_dir.is_symlink() or current_dir.is_junction():
                continue
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        path = Path(entry.path)
                        if entry.is_symlink() or entry.is_junction():
                            continue
                        stat_result = path.lstat()
                    except OSError:
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        pending_empty_dirs.append(path)
                        scan_stack.append(path)
                        continue

                    if not entry.is_file(follow_symlinks=False):
                        continue

                    total_size += stat_result.st_size
                    file_heap.append((stat_result.st_mtime, heap_index, stat_result, path))
                    heap_index += 1
        except OSError:
            continue

    return total_size, file_heap, pending_empty_dirs


def _cleanup_single_temp_dir_by_size(
    temp_dir: Path,
    max_size_bytes: int,
    *,
    journal: FileDownloadCleanupJournal,
) -> tuple[int, int]:
    total_size, file_heap, pending_empty_dirs = _collect_temp_dir_files(temp_dir)
    if total_size <= max_size_bytes:
        return 0, total_size

    heapq.heapify(file_heap)
    deleted_count = 0
    while total_size > max_size_bytes and file_heap:
        _, _, scan_stat, path = heapq.heappop(file_heap)
        try:
            journal.unlink_file(path, expected_stat=scan_stat)
        except OSError as e:
            logger.bind(path=str(path)).warning(t(ERR_TEMP_CLEANER_DELETE_FAILED, message=str(e)))
        else:
            deleted_count += 1
            total_size -= scan_stat.st_size

    for path in reversed(pending_empty_dirs):
        try:
            path.rmdir()
        except OSError:
            continue

    return deleted_count, total_size


def _cleanup_temp_dir_by_size(max_size_bytes: int) -> tuple[int, int]:
    total_deleted_count = 0
    largest_current_size = 0
    journal = get_file_download_cleanup_journal()

    try:
        if max_size_bytes <= 0 or not TEMP_DIR.exists():
            return 0, 0

        temp_root = TEMP_DIR.absolute()
        if temp_root.is_symlink() or temp_root.is_junction():
            return 0, 0
        temp_root = temp_root.resolve(strict=False)

        with os.scandir(temp_root) as entries:
            for entry in entries:
                try:
                    path = Path(entry.path)
                    if entry.is_symlink() or entry.is_junction() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue

                deleted_count, current_size = _cleanup_single_temp_dir_by_size(
                    path,
                    max_size_bytes,
                    journal=journal,
                )
                total_deleted_count += deleted_count
                largest_current_size = max(largest_current_size, current_size)
    except OSError:
        return 0, 0
    finally:
        cleanup_file_download_tokens(replay_removals=True)

    return total_deleted_count, largest_current_size


async def _get_active_temp_dir_max_size_mb() -> int | None:
    async with AsyncSessionLocal() as db:
        settings = await system_setting_crud.get_runtime_settings(db)
        return settings.temp_dir_max_size_mb


async def background_temp_cleaner(interval_seconds: int = 10):
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await recover_user_file_cleanup(db)
            max_size_mb = await _get_active_temp_dir_max_size_mb()
            max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb is not None else 0
            deleted_count, current_size = await asyncio.to_thread(
                _cleanup_temp_dir_by_size,
                max_size_bytes,
            )
            if deleted_count > 0:
                logger.bind(deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes).info(t(MSG_TEMP_CLEANER_CLEARED, deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes))
        except Exception as e:
            logger.error(t(ERR_TEMP_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(interval_seconds)


def _reconcile_file_download_tokens_sync(existing_uids: set[str], *, records_before: float) -> int:
    missing_under: list[Path] = []
    for configured_root in (TEMP_DIR, AUDIT_DIR):
        root = Path(configured_root).absolute()
        if root.is_symlink() or root.is_junction():
            continue
        missing_under.append(root.resolve(strict=False))

    return cleanup_file_download_tokens(
        replay_removals=True,
        existing_uids=existing_uids,
        missing_under=missing_under,
        records_before=records_before,
    )


async def reconcile_file_download_tokens(db: AsyncSession) -> int:
    now_timestamp = time()
    existing_uids = await user_crud.list_uids(db)
    await db.commit()
    return await asyncio.to_thread(
        _reconcile_file_download_tokens_sync,
        existing_uids,
        records_before=now_timestamp - FILE_DOWNLOAD_TOKEN_RECONCILE_GRACE_SECONDS,
    )


async def background_file_download_token_cleaner(
    *,
    interval_seconds: float = FILE_DOWNLOAD_TOKEN_RECONCILE_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await reconcile_file_download_tokens(db)
        except Exception:
            logger.exception("File download token reconciliation failed")

        await asyncio.sleep(interval_seconds)


async def cleanup_audit_history(
    db: AsyncSession,
    *,
    audit_root: str | Path = AUDIT_DIR,
) -> AuditHistoryCleanupResult:
    now_timestamp = time()
    retention_days = await system_setting_crud.get_audit_retention_days(db)
    completed_before = get_local_time() - timedelta(days=retention_days)
    records = await audit_crud.list_history_cleanup_records(db, completed_before=completed_before)
    await db.commit()

    referenced_paths: set[str] = set()
    protected_paths: set[str] = set()
    terminal_by_path: dict[Path, list[AuditHistoryCleanupRecord]] = {}
    records_without_context: list[AuditHistoryCleanupRecord] = []
    for record in records:
        context_path = Path(record.context_file_path).absolute() if record.context_file_path is not None else None
        if context_path is not None:
            referenced_paths.add(str(context_path))
        if context_path is not None and record.status in AUDIT_TERMINAL_STATUSES:
            terminal_by_path.setdefault(context_path, []).append(record)
        if context_path is not None and record.status not in AUDIT_TERMINAL_STATUSES:
            protected_paths.add(str(context_path))
        if context_path is None and record.status in AUDIT_TERMINAL_STATUSES:
            records_without_context.append(record)
        if record.status == AuditRecordStatus.PREPARING:
            expected_path = get_audit_file_path(record.uid, record.id, audit_root=audit_root).absolute()
            protected_paths.add(str(expected_path))

    plan = await asyncio.to_thread(
        plan_audit_storage_cleanup,
        retention_days=retention_days,
        audit_root=audit_root,
        referenced_paths=referenced_paths,
        protected_paths=protected_paths,
        now_timestamp=now_timestamp,
    )
    file_cleanup = AuditCleanupResult(failed_paths=dict(plan.failed_paths))
    work_items: list[_AuditHistoryCleanupWork] = []
    for candidate in plan.candidates:
        related_records = tuple(terminal_by_path.get(candidate.path, ()))
        if (candidate.file_stat is None or related_records) and not _audit_history_records_completed_before(
            related_records,
            completed_before,
        ):
            continue
        work_items.append(_AuditHistoryCleanupWork(candidate=candidate, records=related_records))
    work_items.extend(_AuditHistoryCleanupWork(candidate=None, records=(record,)) for record in records_without_context)

    deleted_database_records = 0
    for batch_start in range(0, len(work_items), AUDIT_HISTORY_CLEANUP_BATCH_SIZE):
        batch = work_items[batch_start : batch_start + AUDIT_HISTORY_CLEANUP_BATCH_SIZE]
        retention_unchanged, batch_deleted_records, batch_cleanup = await _cleanup_audit_history_batch(
            db,
            batch=batch,
            retention_days=retention_days,
        )
        if not retention_unchanged:
            break
        deleted_database_records += batch_deleted_records
        file_cleanup.deleted_files.extend(batch_cleanup.deleted_files)
        file_cleanup.missing_referenced_files.extend(batch_cleanup.missing_referenced_files)
        file_cleanup.failed_paths.update(batch_cleanup.failed_paths)

    if file_cleanup.deleted_files or file_cleanup.missing_referenced_files:
        await asyncio.to_thread(cleanup_file_download_tokens, replay_removals=True)

    return AuditHistoryCleanupResult(
        deleted_database_records=deleted_database_records,
        file_cleanup=file_cleanup,
    )


async def _cleanup_audit_storage_with_cancellation(
    candidates: list[AuditFileCleanupCandidate],
) -> AuditCleanupResult:
    removal_journal = get_file_download_cleanup_journal()
    cleanup_task = asyncio.create_task(asyncio.to_thread(cleanup_audit_storage, candidates, removal_journal=removal_journal))
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not cleanup_task.cancelled():
            cleanup_task.exception()
        raise


async def _cleanup_audit_history_batch(
    db: AsyncSession,
    *,
    batch: list[_AuditHistoryCleanupWork],
    retention_days: int,
) -> tuple[bool, int, AuditCleanupResult]:
    async with db.begin():
        connection = await db.connection()
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        current_retention_days = await system_setting_crud.get_audit_retention_days(db, lock_for_update=True)
        if current_retention_days != retention_days:
            return False, 0, AuditCleanupResult()

        batch_records = [record for work in batch for record in work.records]
        unchanged_record_ids = await audit_crud.lock_unchanged_history_records(db, records=batch_records)
        unchanged_work = [work for work in batch if all(record.id in unchanged_record_ids for record in work.records)]
        candidates = [work.candidate for work in unchanged_work if work.candidate is not None]
        batch_cleanup = await _cleanup_audit_storage_with_cancellation(candidates) if candidates else AuditCleanupResult()
        completed_file_paths = {Path(path).absolute() for path in batch_cleanup.deleted_files}
        completed_file_paths.update(Path(path).absolute() for path in batch_cleanup.missing_referenced_files)
        record_ids: set[int] = set()
        for work in unchanged_work:
            candidate = work.candidate
            if candidate is not None and candidate.path not in completed_file_paths:
                continue
            record_ids.update(record.id for record in work.records)
        deleted_database_records = await audit_crud.delete_records(db, audit_record_ids=record_ids) if record_ids else 0
        return True, deleted_database_records, batch_cleanup


async def background_audit_cleaner(*, interval_seconds: float = AUDIT_HISTORY_CLEANUP_INTERVAL_SECONDS) -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await cleanup_audit_history(db, audit_root=AUDIT_DIR)
            failed_file_count = len(result.file_cleanup.failed_paths)
            if failed_file_count:
                logger.warning("Audit history cleanup failed for {} files", failed_file_count)
        except Exception:
            logger.exception("Audit history cleanup failed")

        await asyncio.sleep(interval_seconds)


async def _cleanup_system_log_batch() -> int | None:
    async with AsyncSessionLocal() as db:
        await ensure_sqlite_outer_transaction(db)
        policy = await system_setting_crud.get_log_retention(db)
        retention_days, max_entries = policy
        now = get_local_time()
        log_ids = await system_log_crud.list_old_log_ids(
            db,
            cutoff=now - timedelta(days=retention_days),
            max_entries=max_entries,
            batch_size=SYSTEM_LOG_CLEANUP_BATCH_SIZE,
            written_before=now - timedelta(seconds=SYSTEM_LOG_REALTIME_GRACE_SECONDS),
        )

    if not log_ids:
        return 0

    async with AsyncSessionLocal() as db, db.begin():
        connection = await db.connection()
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        current_policy = await system_setting_crud.get_log_retention(db, lock_for_update=True)
        # 更宽松设置已保存时，不按旧规则删除。
        if current_policy != policy:
            return None
        return await system_log_crud.delete_log_ids(db, ids=log_ids)


def _list_old_log_files(log_path: Path, *, cutoff: float, batch_size: int) -> list[Path]:
    if batch_size <= 0:
        return []

    timestamp_pattern = r"\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}"
    service_pattern = rf"{re.escape(log_path.stem)}(?:\.\d+)?{timestamp_pattern}(?:\.\d+)?{re.escape(log_path.suffix)}\.zip"
    service_log_pattern = rf"{re.escape(log_path.stem)}(?:\.\d+)?{re.escape(log_path.suffix)}"
    tools_log_path = Path(TOOLS_LOG_FILENAME)
    tools_pattern = rf"{re.escape(tools_log_path.stem)}(?:\.\d+)?{timestamp_pattern}(?:\.\d+)?{re.escape(tools_log_path.suffix)}"
    tools_log_pattern = rf"{re.escape(tools_log_path.stem)}(?:\.\d+)?{re.escape(tools_log_path.suffix)}"
    candidates: list[Path] = []

    try:
        entries = os.scandir(log_path.parent)
    except FileNotFoundError:
        return []

    with entries:
        for entry in entries:
            if all(re.fullmatch(pattern, entry.name) is None for pattern in (service_pattern, service_log_pattern, tools_pattern, tools_log_pattern)):
                continue

            path = Path(entry.path)
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                modified_at = entry.stat(follow_symlinks=False).st_mtime
            except OSError as e:
                logger.bind(path=str(path)).warning(t(ERR_LOG_CLEANER_FAILED, message=str(e)))
                continue

            if modified_at >= cutoff:
                continue

            candidates.append(path)
            if len(candidates) >= batch_size:
                return candidates

    return candidates


def _unlink_inactive_log_file(path: Path, *, cutoff: float) -> bool:
    if os.name == "nt":
        # Windows 的共享或锁冲突表示文件仍在使用，正常跳过。
        try:
            path.unlink()
        except OSError as error:
            if getattr(error, "winerror", None) in (32, 33):
                return False
            raise
        return True

    # 仅在取得独占锁后删除，仍被写入器持有共享锁的文件会跳过。
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False

        fd_stat = os.fstat(fd)
        path_stat = path.stat(follow_symlinks=False)
        if not S_ISREG(fd_stat.st_mode) or not S_ISREG(path_stat.st_mode):
            return False
        if fd_stat.st_dev != path_stat.st_dev or fd_stat.st_ino != path_stat.st_ino:
            return False
        if fd_stat.st_mtime >= cutoff or path_stat.st_mtime >= cutoff:
            return False
        path.unlink()
        return True
    finally:
        os.close(fd)


def _delete_log_files(paths: list[Path], *, cutoff: float) -> int:
    deleted_count = 0
    for path in paths:
        try:
            with log_file_rotation_lock(path.parent, blocking=False) as acquired:
                if not acquired:
                    continue
                # 同一锁覆盖轮转，避免在轮转过程中检查或删除路径。
                stat_result = path.stat(follow_symlinks=False)
                if not S_ISREG(stat_result.st_mode) or stat_result.st_mtime >= cutoff:
                    continue
                if not _unlink_inactive_log_file(path, cutoff=cutoff):
                    continue
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.bind(path=str(path)).warning(t(ERR_LOG_CLEANER_FAILED, message=str(e)))
            continue

        deleted_count += 1

    return deleted_count


async def _cleanup_log_file_batch() -> int | None:
    async with AsyncSessionLocal() as db:
        await ensure_sqlite_outer_transaction(db)
        retention_days, _ = await system_setting_crud.get_log_retention(db)

    cutoff = (get_local_time() - timedelta(days=retention_days)).timestamp()
    log_path = Path(os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)))
    paths = await asyncio.to_thread(
        _list_old_log_files,
        log_path,
        cutoff=cutoff,
        batch_size=SYSTEM_LOG_CLEANUP_BATCH_SIZE,
    )
    if not paths:
        return 0

    async with AsyncSessionLocal() as db, db.begin():
        connection = await db.connection()
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        current_retention_days, _ = await system_setting_crud.get_log_retention(db, lock_for_update=True)
        if current_retention_days != retention_days:
            return None
        return await asyncio.to_thread(_delete_log_files, paths, cutoff=cutoff)


async def background_log_cleaner(*, interval_seconds: float = SYSTEM_LOG_CLEANUP_INTERVAL_SECONDS) -> None:
    while True:
        pause_seconds = interval_seconds
        try:
            deleted_count = await _cleanup_system_log_batch()
            if deleted_count is None or deleted_count == SYSTEM_LOG_CLEANUP_BATCH_SIZE:
                pause_seconds = SYSTEM_LOG_CLEANUP_BATCH_PAUSE_SECONDS
        except Exception as e:
            logger.error(t(ERR_LOG_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(pause_seconds)


async def background_log_file_cleaner(*, interval_seconds: float = SYSTEM_LOG_CLEANUP_INTERVAL_SECONDS) -> None:
    while True:
        pause_seconds = interval_seconds
        try:
            deleted_count = await _cleanup_log_file_batch()
            if deleted_count is None or deleted_count == SYSTEM_LOG_CLEANUP_BATCH_SIZE:
                pause_seconds = SYSTEM_LOG_CLEANUP_BATCH_PAUSE_SECONDS
        except Exception as e:
            logger.error(t(ERR_LOG_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(pause_seconds)
