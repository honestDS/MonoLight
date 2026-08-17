"""管理系统 JWT 签名密钥和渠道加密密钥的生成、旧配置迁移、文件锁、校验及持久化读取。

本模块不处理用户认证、JWT 业务载荷或密码。
"""

import errno
import json
import os
import secrets
import stat
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.constants import (
    ERR_SYSTEM_SECRETS_FILE_INVALID,
    ERR_SYSTEM_SECRETS_FILE_MISSING,
    ERR_SYSTEM_SECRETS_MIGRATION_INVALID,
    ERR_SYSTEM_SECRETS_VERSION_UNSUPPORTED,
    SYSTEM_SECRETS_FILE_VERSION,
)
from app.core.i18n import t
from app.core.paths import SYSTEM_SECRETS_LOCK_PATH, SYSTEM_SECRETS_PATH


@dataclass(frozen=True, slots=True)
class SystemSecrets:
    jwt_secret_key: str
    channel_encryption_key: bytes


class SystemSecretsError(RuntimeError):
    def __init__(self, message_key: str, params: Mapping[str, Any] | None = None, **kwargs: Any):
        merged_params = dict(params or {})
        merged_params.update(kwargs)
        self.message_key = message_key
        self.params = merged_params
        super().__init__(t(message_key, **merged_params))


_SYSTEM_SECRETS_FIELDS = {"version", "jwt_secret_key", "channel_encryption_key"}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _raise_file_invalid() -> None:
    raise SystemSecretsError(ERR_SYSTEM_SECRETS_FILE_INVALID)


def _raise_migration_invalid() -> None:
    raise SystemSecretsError(ERR_SYSTEM_SECRETS_MIGRATION_INVALID)


def _path_exists_without_following_symlink(path: Path) -> bool:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        _raise_file_invalid()

    if stat.S_ISLNK(path_stat.st_mode):
        _raise_file_invalid()
    return True


def _ensure_open_path_is_unchanged(path: Path, file_descriptor: int) -> None:
    try:
        path_stat = os.lstat(path)
    except OSError:
        _raise_file_invalid()

    if stat.S_ISLNK(path_stat.st_mode):
        _raise_file_invalid()

    descriptor_stat = os.fstat(file_descriptor)
    if (path_stat.st_dev, path_stat.st_ino) != (descriptor_stat.st_dev, descriptor_stat.st_ino):
        _raise_file_invalid()


def _open_flags(base_flags: int) -> int:
    flags = base_flags
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _load_from_file(secrets_path: Path) -> SystemSecrets:
    if not _path_exists_without_following_symlink(secrets_path):
        raise SystemSecretsError(ERR_SYSTEM_SECRETS_FILE_MISSING)

    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(secrets_path, _open_flags(os.O_RDONLY))
        _ensure_open_path_is_unchanged(secrets_path, file_descriptor)
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as secrets_file:
            file_descriptor = None
            try:
                document = json.load(secrets_file, object_pairs_hook=_object_pairs_to_dict)
            except (UnicodeError, ValueError, RecursionError):
                _raise_file_invalid()
    except FileNotFoundError:
        raise SystemSecretsError(ERR_SYSTEM_SECRETS_FILE_MISSING) from None
    except SystemSecretsError:
        raise
    except (OSError, TypeError):
        _raise_file_invalid()
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)

    return _parse_document(document)


def _object_pairs_to_dict(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError
        document[key] = value
    return document


def _is_hex_key(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _parse_document(document: object) -> SystemSecrets:
    if not isinstance(document, dict) or "version" not in document:
        _raise_file_invalid()

    version = document["version"]
    if type(version) is not int:
        _raise_file_invalid()
    if version != SYSTEM_SECRETS_FILE_VERSION:
        raise SystemSecretsError(ERR_SYSTEM_SECRETS_VERSION_UNSUPPORTED, version=version)

    if set(document) != _SYSTEM_SECRETS_FIELDS:
        _raise_file_invalid()

    jwt_secret_key = document["jwt_secret_key"]
    encryption_key_hex = document["channel_encryption_key"]
    if not isinstance(jwt_secret_key, str) or not jwt_secret_key:
        _raise_file_invalid()
    if not _is_hex_key(encryption_key_hex):
        _raise_file_invalid()

    try:
        channel_encryption_key = bytes.fromhex(encryption_key_hex)
    except ValueError:
        _raise_file_invalid()
    if len(channel_encryption_key) != 32:
        _raise_file_invalid()

    return SystemSecrets(jwt_secret_key, channel_encryption_key)


def load_system_secrets(secrets_path: Path = SYSTEM_SECRETS_PATH) -> SystemSecrets:
    return _load_from_file(secrets_path)


def _system_secrets_from_environment(environment: Mapping[str, str]) -> SystemSecrets:
    jwt_present = "JWT_SECRET_KEY" in environment
    encryption_present = "MONOLIGH_ENCRYPTION_KEY" in environment

    if not jwt_present and not encryption_present:
        jwt_secret_key = secrets.token_urlsafe(48)
        encryption_key_hex = secrets.token_hex(32)
    else:
        if not jwt_present or not encryption_present:
            _raise_migration_invalid()
        jwt_secret_key = environment["JWT_SECRET_KEY"]
        encryption_key_hex = environment["MONOLIGH_ENCRYPTION_KEY"]
        if not isinstance(jwt_secret_key, str) or not jwt_secret_key.strip():
            _raise_migration_invalid()
        if not _is_hex_key(encryption_key_hex):
            _raise_migration_invalid()

    if not isinstance(jwt_secret_key, str) or not jwt_secret_key:
        _raise_migration_invalid()
    try:
        channel_encryption_key = bytes.fromhex(encryption_key_hex)
    except (TypeError, ValueError):
        _raise_migration_invalid()
    if len(channel_encryption_key) != 32:
        _raise_migration_invalid()

    return SystemSecrets(jwt_secret_key, channel_encryption_key)


def _acquire_lock(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EDEADLK):
                    raise
                time.sleep(0.05)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_lock(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _system_secrets_lock(lock_path: Path) -> Iterator[None]:
    _path_exists_without_following_symlink(lock_path)
    file_descriptor: int | None = None
    lock_file: Any | None = None
    lock_acquired = False
    try:
        file_descriptor = os.open(lock_path, _open_flags(os.O_CREAT | os.O_RDWR), 0o666)
        _ensure_open_path_is_unchanged(lock_path, file_descriptor)
        lock_file = os.fdopen(file_descriptor, "r+b", closefd=True)
        file_descriptor = None

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.seek(0)
            lock_file.write(b"\0")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        _acquire_lock(lock_file)
        lock_acquired = True
        try:
            yield
        finally:
            if lock_acquired:
                _release_lock(lock_file)
    finally:
        if lock_file is not None:
            lock_file.close()
        elif file_descriptor is not None:
            os.close(file_descriptor)


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return

    directory_descriptor: int | None = None
    try:
        directory_descriptor = os.open(path.parent, _open_flags(os.O_RDONLY))
        os.fsync(directory_descriptor)
    except OSError:
        pass
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _write_system_secrets(secrets_path: Path, system_secrets: SystemSecrets) -> bool:
    payload = json.dumps(
        {
            "version": SYSTEM_SECRETS_FILE_VERSION,
            "jwt_secret_key": system_secrets.jwt_secret_key,
            "channel_encryption_key": system_secrets.channel_encryption_key.hex(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )

    temp_path: Path | None = None
    file_descriptor: int | None = None
    try:
        while True:
            candidate_path = secrets_path.parent / f".system-secrets-{uuid.uuid4().hex}.tmp"
            try:
                file_descriptor = os.open(
                    candidate_path,
                    _open_flags(os.O_CREAT | os.O_EXCL | os.O_WRONLY),
                    0o666,
                )
            except FileExistsError:
                continue
            temp_path = candidate_path
            break

        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as temp_file:
            file_descriptor = None
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if _path_exists_without_following_symlink(secrets_path):
            return False
        os.replace(temp_path, secrets_path)
        temp_path = None
        _fsync_parent_directory(secrets_path)
        return True
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def initialize_system_secrets(
    secrets_path: Path = SYSTEM_SECRETS_PATH,
    lock_path: Path = SYSTEM_SECRETS_LOCK_PATH,
    environment: Mapping[str, str] | None = None,
) -> SystemSecrets:
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _system_secrets_lock(lock_path):
        try:
            return load_system_secrets(secrets_path)
        except SystemSecretsError as error:
            if error.message_key != ERR_SYSTEM_SECRETS_FILE_MISSING:
                raise

        system_secrets = _system_secrets_from_environment(environment if environment is not None else os.environ)
        _write_system_secrets(secrets_path, system_secrets)
        return load_system_secrets(secrets_path)


def get_jwt_secret_key() -> str:
    return load_system_secrets().jwt_secret_key


def get_channel_encryption_key() -> bytes:
    return load_system_secrets().channel_encryption_key


__all__ = [
    "SystemSecrets",
    "SystemSecretsError",
    "initialize_system_secrets",
    "load_system_secrets",
    "get_jwt_secret_key",
    "get_channel_encryption_key",
]
