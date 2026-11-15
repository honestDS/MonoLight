import asyncio
import errno
import heapq
import os
import re
import shutil
from datetime import timedelta
from pathlib import Path
from stat import S_ISREG

from app.core.constants import (
    ERR_LOG_CLEANER_FAILED,
    ERR_TEMP_CLEANER_DELETE_FAILED,
    ERR_TEMP_CLEANER_FAILED,
    MSG_TEMP_CLEANER_CLEARED,
    SYSTEM_LOG_CLEANUP_BATCH_PAUSE_SECONDS,
    SYSTEM_LOG_CLEANUP_BATCH_SIZE,
    SYSTEM_LOG_CLEANUP_INTERVAL_SECONDS,
    SYSTEM_LOG_REALTIME_GRACE_SECONDS,
)
from app.core.crud.system.log import system_log_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.i18n import t
from app.core.log import get_logger, log_file_rotation_lock
from app.core.paths import DEFAULT_LOG_FILE_PATH, TEMP_DIR, TOOLS_LOG_FILENAME
from app.core.user.file_cleanup import recover_user_file_cleanup
from app.core.utils.time import get_local_time
from app.providers.database import AsyncSessionLocal, ensure_sqlite_outer_transaction

logger = get_logger(__name__)


def _remove_temp_path(path: Path) -> int:
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    path.unlink()
    return 1


def _collect_temp_dir_files(temp_dir: Path) -> tuple[int, list[tuple[float, int, int, Path]], list[Path]]:
    total_size = 0
    file_heap: list[tuple[float, int, int, Path]] = []
    pending_empty_dirs: list[Path] = []
    scan_stack = [temp_dir]
    heap_index = 0

    while scan_stack:
        current_dir = scan_stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        pending_empty_dirs.append(path)
                        scan_stack.append(path)
                        continue

                    if not entry.is_file(follow_symlinks=False):
                        continue

                    file_size = stat_result.st_size
                    total_size += file_size
                    file_heap.append((stat_result.st_mtime, heap_index, file_size, path))
                    heap_index += 1
        except OSError:
            continue

    return total_size, file_heap, pending_empty_dirs


def _cleanup_single_temp_dir_by_size(temp_dir: Path, max_size_bytes: int) -> tuple[int, int]:
    total_size, file_heap, pending_empty_dirs = _collect_temp_dir_files(temp_dir)
    if total_size <= max_size_bytes:
        return 0, total_size

    heapq.heapify(file_heap)
    deleted_count = 0
    while total_size > max_size_bytes and file_heap:
        _, _, file_size, path = heapq.heappop(file_heap)
        try:
            deleted_count += _remove_temp_path(path)
            total_size -= file_size
        except OSError as e:
            logger.bind(path=str(path)).warning(t(ERR_TEMP_CLEANER_DELETE_FAILED, message=str(e)))

    for path in reversed(pending_empty_dirs):
        try:
            path.rmdir()
        except OSError:
            continue

    return deleted_count, total_size


def _cleanup_temp_dir_by_size(max_size_bytes: int) -> tuple[int, int]:
    if max_size_bytes <= 0 or not TEMP_DIR.exists():
        return 0, 0

    total_deleted_count = 0
    largest_current_size = 0

    try:
        with os.scandir(TEMP_DIR) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue

                deleted_count, current_size = _cleanup_single_temp_dir_by_size(Path(entry.path), max_size_bytes)
                total_deleted_count += deleted_count
                largest_current_size = max(largest_current_size, current_size)
    except OSError:
        return 0, 0

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
            if max_size_mb is not None:
                max_size_bytes = max_size_mb * 1024 * 1024
                deleted_count, current_size = await asyncio.to_thread(_cleanup_temp_dir_by_size, max_size_bytes)
                if deleted_count > 0:
                    logger.bind(deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes).info(t(MSG_TEMP_CLEANER_CLEARED, deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes))
        except Exception as e:
            logger.error(t(ERR_TEMP_CLEANER_FAILED, message=str(e)))

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
