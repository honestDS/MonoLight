import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.tools.send_file_to_user as file_token
from app.models.session import ChatSession
from app.models.user import User, UserFileCleanup
from tests.integration.users import test_user_management_permissions as user_management_permissions

ADMIN_DELETE = user_management_permissions.ADMIN_DELETE
_create_directory_link = user_management_permissions._create_directory_link
_request = user_management_permissions._request
_token = user_management_permissions._token
api_app = user_management_permissions.api_app
cleanup_user_files = user_management_permissions.cleanup_user_files
db_session = user_management_permissions.db_session
file_cleanup = user_management_permissions.file_cleanup
password_hash = user_management_permissions.password_hash
recover_user_file_cleanup = user_management_permissions.recover_user_file_cleanup
setup_session_factory = user_management_permissions.setup_session_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate_file", (False, True), ids=("normal-delete", "recreate-same-path"))
async def test_user_delete_removes_owned_and_legacy_temp_files_without_cross_user_or_traversal(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recreate_file: bool,
) -> None:
    legacy_session_id = "legacy-owned"
    peer_named_session_id = "peer-user"
    posix_escape_session_id = "../outside-posix"
    windows_escape_session_id = r"..\outside-windows"
    db_session.add_all(
        [
            ChatSession(session_id=legacy_session_id, uid="normal-user"),
            ChatSession(session_id=peer_named_session_id, uid="normal-user"),
            ChatSession(session_id=posix_escape_session_id, uid="normal-user"),
            ChatSession(session_id=windows_escape_session_id, uid="normal-user"),
        ]
    )
    await db_session.commit()

    temp_root = tmp_path / "temp"
    normal_file = temp_root / "temp_normal-user" / "nested" / "normal.txt"
    legacy_file = temp_root / f"temp_{legacy_session_id}" / "nested" / "legacy.txt"
    peer_file = temp_root / f"temp_{peer_named_session_id}" / "nested" / "peer.txt"
    escape_files = (
        tmp_path / "outside-posix" / "nested" / "posix.txt",
        tmp_path / "outside-windows" / "nested" / "windows.txt",
    )
    shared_file = tmp_path / "shared" / "shared.txt"
    for path in (normal_file, legacy_file, peer_file, shared_file, *escape_files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    token_dir = tmp_path / "file_download_tokens"
    monkeypatch.setattr(file_token, "FILE_TOKEN_DIR", token_dir)
    tokens = {
        "normal_file": file_token._encode_token({"uid": "normal-user", "path": str(normal_file), "id": "normal-file"}),
        "normal_shared": file_token._encode_token({"uid": "normal-user", "path": str(shared_file), "id": "normal-shared"}),
        "peer_shared": file_token._encode_token({"uid": "peer-user", "path": str(shared_file), "id": "peer-shared"}),
        "peer_deleted": file_token._encode_token({"uid": "peer-user", "path": str(normal_file), "id": "peer-deleted"}),
    }
    assert all(token.startswith("v2.") for token in tokens.values())
    token_records = {name: token_dir / f"{token.split('.')[1]}.json" for name, token in tokens.items()}
    assert all(record.is_file() for record in token_records.values())

    recreated_token: str | None = None
    recreated_record: Path | None = None
    if recreate_file:
        original_remove_paths = file_cleanup._remove_user_file_cleanup_paths

        def _recreate_after_remove(directory_names: list[str]) -> None:
            nonlocal recreated_record, recreated_token
            original_remove_paths(directory_names)
            normal_file.parent.mkdir(parents=True, exist_ok=True)
            normal_file.write_text("recreated-normal-content", encoding="utf-8")
            with pytest.raises(FileNotFoundError):
                file_token.resolve_file_token(tokens["peer_deleted"], uid="peer-user")
            recreated_token = file_token._encode_token({"uid": "peer-user", "path": str(normal_file), "id": "peer-recreated"})
            recreated_record = token_dir / f"{recreated_token.split('.')[1]}.json"
            recreated_mtime_ns = time.time_ns() + 1_000_000_000
            os.utime(recreated_record, ns=(recreated_mtime_ns, recreated_mtime_ns))

        monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", _recreate_after_remove)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    assert response.json()["code"] == 200
    if recreate_file:
        assert normal_file.read_text(encoding="utf-8") == "recreated-normal-content"
        assert normal_file.parents[1].is_dir()
        assert recreated_token is not None
        assert recreated_token.startswith("v2.")
        assert recreated_record is not None
        assert recreated_record.is_file()
        assert file_token.resolve_file_token(recreated_token, uid="peer-user") == normal_file.resolve()
    else:
        assert not normal_file.exists()
        assert not normal_file.parents[1].exists()
    assert not legacy_file.exists()
    assert not legacy_file.parents[1].exists()
    assert peer_file.is_file()
    assert peer_file.parents[1].is_dir()
    assert all(path.is_file() for path in escape_files)
    assert shared_file.is_file()
    assert not token_records["normal_file"].exists()
    assert not token_records["normal_shared"].exists()
    assert not token_records["peer_deleted"].exists()
    assert token_records["peer_shared"].is_file()
    assert file_token.resolve_file_token(tokens["peer_shared"], uid="peer-user") == shared_file.resolve()

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        normal_sessions = await session.scalars(select(ChatSession).where(ChatSession.uid == "normal-user"))
        assert normal_sessions.all() == []
        assert await session.scalar(select(ChatSession).where(ChatSession.session_id == peer_named_session_id)) is None
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None


@pytest.mark.asyncio
async def test_user_delete_retries_pending_file_cleanup_after_disk_failure(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_file = tmp_path / "temp" / "temp_normal-user" / "nested" / "normal.txt"
    peer_file = tmp_path / "temp" / "temp_peer-user" / "nested" / "peer.txt"
    for path in (normal_file, peer_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths

    def failing_remove_paths(directory_names: list[str]) -> None:
        raise PermissionError(f"injected failure for {directory_names[0]}")

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", failing_remove_paths)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert failed.status_code == 500
    assert normal_file.is_file()
    assert peer_file.is_file()

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        pending_cleanup = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user"))
        assert pending_cleanup is not None

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", original_remove_paths)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        retried = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert retried.status_code == 200
    assert retried.json()["code"] == 200
    assert not normal_file.exists()
    assert peer_file.is_file()

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None


@pytest.mark.asyncio
async def test_recover_user_file_cleanup_continues_after_one_disk_failure(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_uid = "deleted-first"
    second_uid = "deleted-second"
    db_session.add_all(
        [
            UserFileCleanup(uid=first_uid, legacy_session_ids=[]),
            UserFileCleanup(uid=second_uid, legacy_session_ids=[]),
        ]
    )
    await db_session.commit()

    file_paths = {uid: tmp_path / "temp" / f"temp_{uid}" / "nested" / "attachment.txt" for uid in (first_uid, second_uid)}
    for uid, path in file_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(uid, encoding="utf-8")

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths

    def fail_first_remove(directory_names: list[str]) -> None:
        if directory_names[0] == first_uid:
            raise PermissionError(f"injected failure for {first_uid}")
        original_remove_paths(directory_names)

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", fail_first_remove)
    assert await recover_user_file_cleanup(db_session) == 1
    assert file_paths[first_uid].is_file()
    assert not file_paths[second_uid].exists()

    async with setup_session_factory() as session:
        pending = await session.scalars(select(UserFileCleanup).order_by(UserFileCleanup.uid))
        assert [cleanup.uid for cleanup in pending.all()] == [first_uid]

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", original_remove_paths)
    assert await recover_user_file_cleanup(db_session) == 1
    assert not file_paths[first_uid].exists()
    assert await recover_user_file_cleanup(db_session) == 0

    async with setup_session_factory() as session:
        pending = await session.scalars(select(UserFileCleanup))
        assert pending.all() == []


@pytest.mark.asyncio
async def test_file_cleanup_boundary_keeps_pending_record_for_existing_user(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "temp" / "temp_normal-user" / "nested" / "attachment.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"existing-user-content")
    db_session.add(UserFileCleanup(uid="normal-user", legacy_session_ids=[]))
    await db_session.commit()

    assert await recover_user_file_cleanup(db_session) == 0
    assert file_path.read_bytes() == b"existing-user-content"

    async with setup_session_factory() as session:
        pending = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user"))
        assert pending is not None
        assert pending.legacy_session_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    (
        "deleted-file-owner",
        pytest.param(
            "DELETED-FILE-OWNER",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only"),
        ),
    ),
    ids=("exact-session-reuse", "uppercase-session-alias"),
)
async def test_file_cleanup_boundary_preserves_main_directory_reused_by_peer_session(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    session_id: str,
) -> None:
    deleted_uid = "deleted-file-owner"
    db_session.add_all(
        [
            ChatSession(session_id=session_id, uid="peer-user"),
            UserFileCleanup(uid=deleted_uid, legacy_session_ids=[]),
        ]
    )
    await db_session.commit()

    reused_file = tmp_path / "temp" / f"temp_{deleted_uid}" / "nested" / "reused.txt"
    reused_file.parent.mkdir(parents=True, exist_ok=True)
    reused_file.write_bytes(b"reused-content")

    assert await recover_user_file_cleanup(db_session) == 0
    assert reused_file.read_bytes() == b"reused-content"

    async with setup_session_factory() as session:
        pending = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == deleted_uid))
        assert pending is not None
        assert pending.legacy_session_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_session_id", "current_session_id", "session_owner"),
    (
        ("reused-session", "reused-session", "peer-user"),
        ("peer-user", None, None),
        pytest.param(
            "REUSED-SESSION",
            "reused-session",
            "peer-user",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only"),
        ),
    ),
    ids=("peer-session-reuse", "active-user-uid", "windows-case-alias"),
)
async def test_file_cleanup_boundary_preserves_reused_legacy_directory(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    legacy_session_id: str,
    current_session_id: str | None,
    session_owner: str | None,
) -> None:
    deleted_uid = "deleted-file-owner"
    if current_session_id is not None:
        assert session_owner is not None
        db_session.add(ChatSession(session_id=current_session_id, uid=session_owner))
    db_session.add(UserFileCleanup(uid=deleted_uid, legacy_session_ids=[legacy_session_id]))
    await db_session.commit()

    deleted_file = tmp_path / "temp" / f"temp_{deleted_uid}" / "nested" / "deleted.txt"
    reused_file = tmp_path / "temp" / f"temp_{legacy_session_id}" / "nested" / "reused.txt"
    for path, content in ((deleted_file, b"deleted-content"), (reused_file, b"reused-content")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert await recover_user_file_cleanup(db_session) == 1
    assert not deleted_file.exists()
    assert reused_file.read_bytes() == b"reused-content"

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == deleted_uid)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only")
@pytest.mark.parametrize("session_id", ("peer-user.", "peer-user ", "PEER-USER"))
async def test_file_cleanup_boundary_windows_alias_does_not_remove_peer_files(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    session_id: str,
) -> None:
    db_session.add(ChatSession(session_id=session_id, uid="normal-user"))
    await db_session.commit()

    normal_file = tmp_path / "temp" / "temp_normal-user" / "nested" / "normal.txt"
    peer_file = tmp_path / "temp" / "temp_peer-user" / "nested" / "peer.txt"
    normal_file.parent.mkdir(parents=True, exist_ok=True)
    peer_file.parent.mkdir(parents=True, exist_ok=True)
    normal_file.write_bytes(b"normal-content")
    peer_file.write_bytes(b"peer-content")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )

    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert not normal_file.exists()
    assert peer_file.read_bytes() == b"peer-content"

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_removes_user_directory_link_without_following_target(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "deleted-directory-link"
    target_file = tmp_path / "outside-user-directory" / "nested" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    user_link = tmp_path / "temp" / f"temp_{uid}"
    _create_directory_link(user_link, target_file.parents[1])

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 1

    assert not user_link.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_does_not_traverse_internal_directory_link(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "deleted-internal-link"
    user_directory = tmp_path / "temp" / f"temp_{uid}"
    owned_file = user_directory / "owned.txt"
    owned_file.parent.mkdir(parents=True, exist_ok=True)
    owned_file.write_bytes(b"owned-content")
    target_file = tmp_path / "outside-internal-directory" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    _create_directory_link(user_directory / "linked-directory", target_file.parent)

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 1

    assert not owned_file.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_rejects_linked_temp_root_and_keeps_pending_cleanup(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "linked-temp-root-user"
    target_file = tmp_path / "outside-temp-root" / f"temp_{uid}" / "nested" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    temp_link = tmp_path / "temp"
    _create_directory_link(temp_link, target_file.parents[2])

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 0

    assert temp_link.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is not None


@pytest.mark.asyncio
async def test_file_cleanup_concurrent_cleanup_same_uid_is_idempotent(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "concurrent-file-cleanup"
    file_path = tmp_path / "temp" / f"temp_{uid}" / "nested" / "attachment.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"concurrent-content")
    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths
    loop = asyncio.get_running_loop()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = threading.Event()
    entry_lock = threading.Lock()
    first_claimed = False

    def synchronized_remove(directory_names: list[str]) -> None:
        nonlocal first_claimed
        with entry_lock:
            is_first = not first_claimed
            if is_first:
                first_claimed = True
        loop.call_soon_threadsafe((first_entered if is_first else second_entered).set)
        if is_first and not release_first.wait(timeout=5):
            raise TimeoutError("timed out waiting to release first cleanup")
        original_remove_paths(directory_names)

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", synchronized_remove)
    async with setup_session_factory() as first_session, setup_session_factory() as second_session:
        first_task = asyncio.create_task(cleanup_user_files(first_session, uid=uid))
        second_task: asyncio.Task[bool] | None = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=1)
            second_task = asyncio.create_task(cleanup_user_files(second_session, uid=uid))
            try:
                await asyncio.wait_for(second_entered.wait(), timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release_first.set()

        tasks = [first_task] if second_task is None else [first_task, second_task]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(not isinstance(result, BaseException) for result in results)
    assert sorted(results) == [False, True]
    assert not file_path.exists()

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None
