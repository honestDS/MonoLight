import asyncio
import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_FILE_ARGUMENT_INVALID, ERR_TEMP_CLEANER_DELETE_FAILED
from app.core.crud.account.file_cleanup import user_file_cleanup_crud
from app.core.crud.account.user import user_crud
from app.core.crud.session.session import session_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import AUDIT_DIR, TEMP_DIR, USER_TEMP_DIR_PREFIX


def _is_safe_directory_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value[-1] not in ". " and all(ord(character) >= 32 for character in value) and not any(character in '<>:"|?*' for character in value) and value not in {".", ".."} and PurePosixPath(value).name == value and PureWindowsPath(value).name == value


def _validate_directory_name(value: object) -> str:
    if not _is_safe_directory_name(value):
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID)
    return value


async def queue_user_file_cleanup(
    db: AsyncSession,
    *,
    uid: str,
    session_ids: list[str],
) -> None:
    _validate_directory_name(uid)

    legacy_session_ids: list[str] = []
    seen: set[str] = set()
    for session_id in session_ids:
        if _is_safe_directory_name(session_id) and session_id != uid and session_id not in seen:
            seen.add(session_id)
            legacy_session_ids.append(session_id)

    active_users = await user_crud.get_multi_by_uids(db, legacy_session_ids)
    active_user_uids = {user.uid for user in active_users}
    legacy_session_ids = [session_id for session_id in legacy_session_ids if session_id not in active_user_uids]

    await user_file_cleanup_crud.enqueue(
        db,
        uid=uid,
        legacy_session_ids=legacy_session_ids,
    )


def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _remove_cleanup_path(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        path.unlink()
        return
    if _is_junction(path):
        path.rmdir()
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_cleanup_directories(root: str | Path, directory_names: list[str]) -> None:
    root_path = Path(root)
    if root_path.is_symlink() or _is_junction(root_path):
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID)
    normalized_root = root_path.resolve(strict=False)

    for directory_name in directory_names:
        target_name = f"{USER_TEMP_DIR_PREFIX}{directory_name}"
        target_path = normalized_root / target_name
        if target_path.parent != normalized_root or target_path.name != target_name:
            raise ParameterException(ERR_FILE_ARGUMENT_INVALID)
        _remove_cleanup_path(target_path)


def _remove_user_file_cleanup_paths(directory_names: list[str]) -> None:
    _remove_cleanup_directories(TEMP_DIR, directory_names)
    _remove_cleanup_directories(AUDIT_DIR, [directory_names[0]])


async def cleanup_user_files(db: AsyncSession, *, uid: str) -> bool:
    cleanup = await user_file_cleanup_crud.get_by_uid(db, uid=uid, lock_for_update=True)
    if cleanup is None:
        return False

    _validate_directory_name(uid)
    stored_session_ids = cleanup.legacy_session_ids
    if not isinstance(stored_session_ids, list):
        raise ParameterException(ERR_FILE_ARGUMENT_INVALID)

    legacy_session_ids: list[str] = []
    seen_directory_keys: set[str] = {os.path.normcase(uid)}
    for session_id in stored_session_ids:
        _validate_directory_name(session_id)
        directory_key = os.path.normcase(session_id)
        if directory_key not in seen_directory_keys:
            seen_directory_keys.add(directory_key)
            legacy_session_ids.append(session_id)

    candidate_names = [uid, *legacy_session_ids]
    case_insensitive = os.name == "nt"
    active_users = await user_crud.get_multi_by_uids(
        db,
        candidate_names,
        case_insensitive=case_insensitive,
        lock_for_update=True,
    )
    active_user_directory_keys = {os.path.normcase(user.uid) for user in active_users}
    uid_directory_key = os.path.normcase(uid)
    if uid_directory_key in active_user_directory_keys:
        return False

    sessions = await session_crud.get_multi_by_session_ids(
        db,
        session_ids=candidate_names,
        case_insensitive=case_insensitive,
        lock_for_update=True,
    )
    reused_session_directory_keys = {os.path.normcase(session.session_id) for session in sessions if session.uid != uid}
    if uid_directory_key in reused_session_directory_keys:
        return False

    protected_directory_keys = active_user_directory_keys | reused_session_directory_keys
    removable_session_ids = [session_id for session_id in legacy_session_ids if os.path.normcase(session_id) not in protected_directory_keys]

    directory_names = [uid, *removable_session_ids]
    await asyncio.to_thread(_remove_user_file_cleanup_paths, directory_names)
    await user_file_cleanup_crud.remove(db, uid=uid)
    await db.commit()
    return True


async def recover_user_file_cleanup(db: AsyncSession) -> int:
    pending = await user_file_cleanup_crud.list_pending(db, limit=100)
    pending_uids = [cleanup.uid for cleanup in pending]
    recovered_count = 0
    logger = get_logger(__name__)

    for uid in pending_uids:
        try:
            if await cleanup_user_files(db, uid=uid):
                recovered_count += 1
        except Exception as exc:
            await db.rollback()
            logger.bind(path=uid).error(t(ERR_TEMP_CLEANER_DELETE_FAILED, message=str(exc)))

    return recovered_count
