import pytest

from app.core.message_platforms.process_lock import ProcessFileLock, ProcessLockError


def test_process_file_lock_allows_only_one_holder(tmp_path):
    lock_path = tmp_path / "message-platform-worker.lock"
    first_lock = ProcessFileLock(lock_path)
    second_lock = ProcessFileLock(lock_path)

    first_lock.acquire()
    try:
        with pytest.raises(ProcessLockError):
            second_lock.acquire()
    finally:
        first_lock.release()

    second_lock.acquire()
    second_lock.release()


def test_process_file_lock_acquire_and_release_are_idempotent(tmp_path):
    lock = ProcessFileLock(tmp_path / "message-platform-worker.lock")

    lock.acquire()
    lock.acquire()
    lock.release()
    lock.release()
