import asyncio
import heapq
import os
import shutil
from datetime import timedelta
from pathlib import Path

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
from app.core.log import get_logger
from app.core.paths import TEMP_DIR
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
