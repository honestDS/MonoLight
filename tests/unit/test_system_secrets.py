from __future__ import annotations

import errno
import json
import logging
import multiprocessing
import os
import stat
import time
from pathlib import Path
from queue import Empty
from typing import Any

import pytest
from jose import jwt

import app.core.system_secrets as system_secrets_module
from app.core.constants import (
    ERR_SYSTEM_SECRETS_FILE_INVALID,
    ERR_SYSTEM_SECRETS_FILE_MISSING,
    ERR_SYSTEM_SECRETS_MIGRATION_INVALID,
    ERR_SYSTEM_SECRETS_VERSION_UNSUPPORTED,
    JWT_ALGORITHM,
    SYSTEM_SECRETS_FILE_VERSION,
)
from app.core.paths import ROOT_DIR, SYSTEM_SECRETS_LOCK_PATH, SYSTEM_SECRETS_PATH
from app.core.system_secrets import SystemSecretsError, initialize_system_secrets, load_system_secrets

VALID_JWT_SECRET = "legacy-jwt-secret-for-system-secrets-tests"
VALID_ENCRYPTION_KEY_HEX = "0123456789abcdef" * 4


def _document_text(**values: object) -> str:
    document: dict[str, object] = {
        "version": SYSTEM_SECRETS_FILE_VERSION,
        "jwt_secret_key": VALID_JWT_SECRET,
        "channel_encryption_key": VALID_ENCRYPTION_KEY_HEX,
    }
    document.update(values)
    return json.dumps(document)


def _initialize_system_secrets_in_process(
    secrets_path: str,
    lock_path: str,
    environment: dict[str, str],
    start_event: Any,
    result_queue: Any,
) -> None:
    start_event.wait()
    try:
        system_secrets = initialize_system_secrets(
            Path(secrets_path),
            Path(lock_path),
            environment=environment,
        )
    except BaseException as error:
        result_queue.put(("error", type(error).__name__, getattr(error, "message_key", None)))
    else:
        result_queue.put(("ok", system_secrets.jwt_secret_key, system_secrets.channel_encryption_key))


def _run_spawn_initializers(
    secrets_path: Path,
    lock_path: Path,
    environment: dict[str, str],
    *,
    count: int = 6,
) -> list[tuple[Any, ...]]:
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_initialize_system_secrets_in_process,
            args=(str(secrets_path), str(lock_path), dict(environment), start_event, result_queue),
        )
        for _ in range(count)
    ]
    deadline = time.monotonic() + 20

    try:
        for process in processes:
            process.start()
        start_event.set()

        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        if any(process.is_alive() for process in processes):
            pytest.fail("spawned system secret initializer did not finish before the timeout")

        results: list[tuple[Any, ...]] = []
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=3))
            except Empty:
                pytest.fail("spawned system secret initializer returned no result")
        return results
    finally:
        start_event.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()


def test_system_secrets_default_paths_are_in_data_directory() -> None:
    assert SYSTEM_SECRETS_PATH.relative_to(ROOT_DIR) == Path("data/system_secrets.json")
    assert SYSTEM_SECRETS_LOCK_PATH.relative_to(ROOT_DIR) == Path("data/system_secrets.lock")


def test_first_initialization_generates_valid_jwt_key_and_channel_key(tmp_path: Path) -> None:
    secrets_path = tmp_path / "data" / "system_secrets.json"
    lock_path = tmp_path / "data" / "system_secrets.lock"

    system_secrets = initialize_system_secrets(secrets_path, lock_path, environment={})

    token = jwt.encode({"sub": "system-secrets-test"}, system_secrets.jwt_secret_key, algorithm=JWT_ALGORITHM)
    assert jwt.decode(token, system_secrets.jwt_secret_key, algorithms=[JWT_ALGORITHM])["sub"] == "system-secrets-test"
    assert len(system_secrets.channel_encryption_key) == 32

    document = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "jwt_secret_key", "channel_encryption_key"}
    assert document["version"] == SYSTEM_SECRETS_FILE_VERSION
    assert document["jwt_secret_key"] == system_secrets.jwt_secret_key
    assert document["channel_encryption_key"] == system_secrets.channel_encryption_key.hex()
    assert lock_path.is_file()
    assert not list(secrets_path.parent.glob(".system-secrets-*.tmp"))

    if os.name == "posix":
        assert stat.S_IMODE(secrets_path.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(lock_path.stat().st_mode) & 0o077 == 0


def test_initialization_atomically_replaces_secret_file_from_same_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    original_replace = system_secrets_module.os.replace
    replace_calls: list[tuple[Path, Path, bool]] = []

    def record_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        replace_calls.append((source_path, Path(destination), source_path.exists()))
        original_replace(source, destination)

    monkeypatch.setattr(system_secrets_module.os, "replace", record_replace)

    initialize_system_secrets(secrets_path, lock_path, environment={})

    assert len(replace_calls) == 1
    source_path, destination_path, source_existed = replace_calls[0]
    assert source_existed
    assert source_path.parent == secrets_path.parent
    assert source_path.name.startswith(".system-secrets-")
    assert source_path.name.endswith(".tmp")
    assert destination_path == secrets_path
    assert not source_path.exists()


def test_posix_initialization_clears_existing_secret_file_group_and_other_permissions(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only test")

    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    initialize_system_secrets(secrets_path, lock_path, environment={})
    original_file_bytes = secrets_path.read_bytes()
    os.chmod(secrets_path, 0o666)

    initialize_system_secrets(secrets_path, lock_path, environment={})

    assert secrets_path.read_bytes() == original_file_bytes
    assert stat.S_IMODE(secrets_path.stat().st_mode) & 0o077 == 0


def test_restart_reuses_existing_bytes_and_ignores_conflicting_environment(tmp_path: Path) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    first = initialize_system_secrets(secrets_path, lock_path, environment={})
    original_file_bytes = secrets_path.read_bytes()

    second = initialize_system_secrets(
        secrets_path,
        lock_path,
        environment={
            "JWT_SECRET_KEY": "conflicting-jwt-secret",
            "MONOLIGH_ENCRYPTION_KEY": "fedcba9876543210" * 4,
        },
    )

    assert second == first
    assert secrets_path.read_bytes() == original_file_bytes


def test_valid_legacy_environment_is_migrated_exactly(tmp_path: Path) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    environment = {
        "JWT_SECRET_KEY": VALID_JWT_SECRET,
        "MONOLIGH_ENCRYPTION_KEY": VALID_ENCRYPTION_KEY_HEX,
    }

    system_secrets = initialize_system_secrets(secrets_path, lock_path, environment=environment)

    assert system_secrets.jwt_secret_key == VALID_JWT_SECRET
    assert system_secrets.channel_encryption_key == bytes.fromhex(VALID_ENCRYPTION_KEY_HEX)
    assert json.loads(secrets_path.read_text(encoding="utf-8")) == {
        "version": SYSTEM_SECRETS_FILE_VERSION,
        "jwt_secret_key": VALID_JWT_SECRET,
        "channel_encryption_key": VALID_ENCRYPTION_KEY_HEX,
    }


@pytest.mark.parametrize(
    ("environment", "input_values"),
    [
        ({"JWT_SECRET_KEY": VALID_JWT_SECRET}, (VALID_JWT_SECRET,)),
        ({"MONOLIGH_ENCRYPTION_KEY": VALID_ENCRYPTION_KEY_HEX}, (VALID_ENCRYPTION_KEY_HEX,)),
        (
            {"JWT_SECRET_KEY": "   ", "MONOLIGH_ENCRYPTION_KEY": VALID_ENCRYPTION_KEY_HEX},
            (VALID_ENCRYPTION_KEY_HEX,),
        ),
        (
            {"JWT_SECRET_KEY": "", "MONOLIGH_ENCRYPTION_KEY": VALID_ENCRYPTION_KEY_HEX},
            (VALID_ENCRYPTION_KEY_HEX,),
        ),
        (
            {"JWT_SECRET_KEY": VALID_JWT_SECRET, "MONOLIGH_ENCRYPTION_KEY": "g" * 64},
            (VALID_JWT_SECRET, "g" * 64),
        ),
        (
            {"JWT_SECRET_KEY": VALID_JWT_SECRET, "MONOLIGH_ENCRYPTION_KEY": "0" * 62},
            (VALID_JWT_SECRET, "0" * 62),
        ),
    ],
    ids=["jwt-only", "encryption-only", "blank-jwt-whitespace", "blank-jwt-empty", "non-hex-encryption", "wrong-length-encryption"],
)
def test_invalid_legacy_environment_is_rejected_without_secret_leaks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    environment: dict[str, str],
    input_values: tuple[str, ...],
) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemSecretsError) as error_info:
            initialize_system_secrets(secrets_path, lock_path, environment=environment)

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_MIGRATION_INVALID
    for input_value in input_values:
        if input_value:
            assert input_value not in str(error_info.value)
            assert input_value not in caplog.text


def test_load_missing_system_secrets_file_raises_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemSecretsError) as error_info:
        load_system_secrets(tmp_path / "missing.json")

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_FILE_MISSING


@pytest.mark.parametrize(
    "file_content",
    [
        "{",
        '{"version":1,"jwt_secret_key":"legacy-jwt-secret",',
        (f'{{"version":1,"jwt_secret_key":"first","jwt_secret_key":"second","channel_encryption_key":"{VALID_ENCRYPTION_KEY_HEX}"}}'),
        json.dumps({"version": SYSTEM_SECRETS_FILE_VERSION, "channel_encryption_key": VALID_ENCRYPTION_KEY_HEX}),
        json.dumps(
            {
                "version": SYSTEM_SECRETS_FILE_VERSION,
                "jwt_secret_key": VALID_JWT_SECRET,
                "channel_encryption_key": VALID_ENCRYPTION_KEY_HEX,
                "extra": True,
            }
        ),
        _document_text(jwt_secret_key=""),
        _document_text(channel_encryption_key="z" * 64),
        _document_text(channel_encryption_key="0" * 62),
    ],
    ids=[
        "damaged-json",
        "truncated-json",
        "duplicate-key",
        "missing-field",
        "extra-field",
        "empty-jwt",
        "non-hex-encryption",
        "wrong-length-encryption",
    ],
)
def test_invalid_system_secrets_file_is_not_overwritten(tmp_path: Path, file_content: str) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    secrets_path.write_text(file_content, encoding="utf-8")
    original_file_bytes = secrets_path.read_bytes()

    with pytest.raises(SystemSecretsError) as error_info:
        initialize_system_secrets(secrets_path, lock_path, environment={})

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_FILE_INVALID
    assert secrets_path.read_bytes() == original_file_bytes


def test_unknown_system_secrets_version_is_rejected_without_overwrite(tmp_path: Path) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    file_content = _document_text(version=SYSTEM_SECRETS_FILE_VERSION + 1)
    secrets_path.write_text(file_content, encoding="utf-8")
    original_file_bytes = secrets_path.read_bytes()

    with pytest.raises(SystemSecretsError) as error_info:
        initialize_system_secrets(secrets_path, lock_path, environment={})

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_VERSION_UNSUPPORTED
    assert secrets_path.read_bytes() == original_file_bytes


def test_system_secrets_symlink_is_rejected(tmp_path: Path) -> None:
    target_path = tmp_path / "target.json"
    secrets_path = tmp_path / "system_secrets.json"
    target_path.write_text(_document_text(), encoding="utf-8")
    try:
        secrets_path.symlink_to(target_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symlink creation is not supported on this platform: {error}")

    with pytest.raises(SystemSecretsError) as error_info:
        load_system_secrets(secrets_path)

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_FILE_INVALID
    assert target_path.read_text(encoding="utf-8") == _document_text()


def test_system_secrets_lock_symlink_is_rejected_without_modifying_target(tmp_path: Path) -> None:
    target_path = tmp_path / "lock-target"
    lock_path = tmp_path / "system_secrets.lock"
    secrets_path = tmp_path / "system_secrets.json"
    target_path.write_bytes(b"lock-target")
    try:
        lock_path.symlink_to(target_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symlink creation is not supported on this platform: {error}")
    original_target_bytes = target_path.read_bytes()

    with pytest.raises(SystemSecretsError) as error_info:
        initialize_system_secrets(secrets_path, lock_path, environment={})

    assert error_info.value.message_key == ERR_SYSTEM_SECRETS_FILE_INVALID
    assert target_path.read_bytes() == original_target_bytes


def test_windows_acquire_lock_reraises_non_competition_error(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only test")

    import msvcrt

    class FakeLockFile:
        def seek(self, _offset: int) -> None:
            pass

        def fileno(self) -> int:
            return 17

    lock_error = OSError(errno.EBADF, "bad file descriptor")

    def raise_bad_file_descriptor(_file_descriptor: int, _mode: int, _size: int) -> None:
        raise lock_error

    def fail_sleep(_seconds: float) -> None:
        pytest.fail("non-competition lock errors must not retry")

    monkeypatch.setattr(msvcrt, "locking", raise_bad_file_descriptor)
    monkeypatch.setattr(system_secrets_module.time, "sleep", fail_sleep)

    with pytest.raises(OSError) as error_info:
        system_secrets_module._acquire_lock(FakeLockFile())

    assert error_info.value is lock_error
    assert error_info.value.errno == errno.EBADF


@pytest.mark.parametrize("use_legacy_environment", [False, True], ids=["generated", "migration"])
def test_spawn_processes_concurrently_initialize_one_persistent_secret_set(
    tmp_path: Path,
    use_legacy_environment: bool,
) -> None:
    secrets_path = tmp_path / "system_secrets.json"
    lock_path = tmp_path / "system_secrets.lock"
    environment = (
        {
            "JWT_SECRET_KEY": VALID_JWT_SECRET,
            "MONOLIGH_ENCRYPTION_KEY": VALID_ENCRYPTION_KEY_HEX,
        }
        if use_legacy_environment
        else {}
    )

    results = _run_spawn_initializers(secrets_path, lock_path, environment)

    assert all(result[0] == "ok" for result in results)
    returned_values = {(result[1], result[2]) for result in results}
    assert len(returned_values) == 1
    final_secrets = load_system_secrets(secrets_path)
    final_value = (final_secrets.jwt_secret_key, final_secrets.channel_encryption_key)
    assert final_value in returned_values
    assert lock_path.is_file()
    if use_legacy_environment:
        assert final_secrets.jwt_secret_key == VALID_JWT_SECRET
        assert final_secrets.channel_encryption_key == bytes.fromhex(VALID_ENCRYPTION_KEY_HEX)
