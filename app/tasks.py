import asyncio
import heapq
import os
import shutil
from pathlib import Path

from app.core.constants import (
    ERR_LOG_CLEANER_FAILED,
    ERR_TEMP_CLEANER_DELETE_FAILED,
    ERR_TEMP_CLEANER_FAILED,
    MSG_LOG_CLEANER_CLEARED,
    MSG_TEMP_CLEANER_CLEARED,
)
from app.core.crud.system.log import system_log_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import TEMP_DIR
from app.providers.database import AsyncSessionLocal

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
            max_size_mb = await _get_active_temp_dir_max_size_mb()
            if max_size_mb is not None:
                max_size_bytes = max_size_mb * 1024 * 1024
                deleted_count, current_size = await asyncio.to_thread(_cleanup_temp_dir_by_size, max_size_bytes)
                if deleted_count > 0:
                    logger.bind(deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes).info(t(MSG_TEMP_CLEANER_CLEARED, deleted_count=deleted_count, current_size=current_size, max_size_bytes=max_size_bytes))
        except Exception as e:
            logger.error(t(ERR_TEMP_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(interval_seconds)


async def background_log_cleaner(days: int = 7):
    while True:
        try:
            async with AsyncSessionLocal() as db:
                deleted_count = await system_log_crud.clear_expired_logs(db, days=days)
                await db.commit()
                if deleted_count > 0:
                    logger.bind(deleted_count=deleted_count, retention_days=days).info(t(MSG_LOG_CLEANER_CLEARED, deleted_count=deleted_count))
        except Exception as e:
            logger.bind(retention_days=days).error(t(ERR_LOG_CLEANER_FAILED, message=str(e)))

        await asyncio.sleep(86400)  # 24 hours
