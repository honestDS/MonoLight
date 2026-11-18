import base64
import io
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import quote

import anyio
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.adapters.weixin_openclaw.media as weixin_media_module
import app.core.security as security_module
import app.core.tools.send_file_to_user as send_file_to_user_module
import app.tasks as tasks_module
from app.adapters.weixin_openclaw.constants import FILE_ITEM_TYPE, IMAGE_ITEM_TYPE, TEXT_ITEM_TYPE
from app.adapters.weixin_openclaw.crypto import aes_ecb_encrypt, pkcs7_pad
from app.api.v1 import files as files_module
from app.core.constants import (
    ERR_FILE_NOT_FOUND,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_READ_ONLY,
    ERR_UNAUTHORIZED,
)
from app.core.i18n import t
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.utils.assistant_files import materialize_generated_images
from app.handler import register_handlers
from app.models.message import InternalGeneratedImage, InternalMessage, Message, MessageRole
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.profile import ProfileConfig
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(tables=[User.__table__, ChatSession.__table__, Message.__table__]) as session_factory:
        async with session_factory() as session:
            yield session


@pytest.fixture(autouse=True)
def isolate_file_token_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"f" * 32)


def _build_app(db_session: AsyncSession, auth_state: dict[str, str | bool]) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(files_module.router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user() -> User:
        return User(
            uid=str(auth_state["uid"]),
            username=str(auth_state["username"]),
            is_superuser=bool(auth_state.get("is_superuser", False)),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


def _build_unauthenticated_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(files_module.router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_upload_files_are_shared_per_user_across_web_sessions(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_one_session = ChatSession(
        session_id="user-1-session-one",
        uid="user-1",
        source="http",
        reply_target_source="http",
    )
    user_two_session = ChatSession(
        session_id="user-1-session-two",
        uid="user-1",
        source="http",
        reply_target_source="http",
    )
    other_user_session = ChatSession(
        session_id="user-2-session-one",
        uid="user-2",
        source="http",
        reply_target_source="http",
    )
    db_session.add_all(
        [
            User(uid="user-1", username="user_one"),
            User(uid="user-2", username="user_two"),
            user_one_session,
            user_two_session,
            other_user_session,
        ]
    )
    await db_session.commit()

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    app = _build_app(db_session, auth_state)
    user_one_dir = get_user_temp_dir(tmp_path, "user-1")
    user_two_dir = get_user_temp_dir(tmp_path, "user-2")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        user_one_uploads = []
        for session_id, content in (
            (user_one_session.session_id, b"user-1-session-one"),
            (user_two_session.session_id, b"user-1-session-two"),
        ):
            response = await client.post(
                "/api/v1/upload",
                data={"session_id": session_id},
                files={"file": ("shared.txt", content, "text/plain")},
            )
            assert response.status_code == 200
            user_one_uploads.append((response.json(), content, session_id))

        for content in (b"user-1-unassigned-one", b"user-1-unassigned-two"):
            response = await client.post(
                "/api/v1/upload",
                files={"file": ("shared.txt", content, "text/plain")},
            )
            assert response.status_code == 200
            user_one_uploads.append((response.json(), content, None))

        user_one_paths = []
        for payload, content, session_id in user_one_uploads:
            path = Path(payload["path"])
            user_one_paths.append(path)
            assert path.is_absolute()
            assert path.parent == user_one_dir
            assert path.read_bytes() == content
            prefix, filename = path.name.split("_", 1)
            assert len(prefix) == 8
            assert all(character in "0123456789abcdef" for character in prefix)
            assert filename == "shared.txt"
            assert payload["filename"] == "shared.txt"
            if session_id is None:
                assert payload["session_id"].startswith("unassigned_")
                assert not get_user_temp_dir(tmp_path, payload["session_id"]).exists()
            else:
                assert payload["session_id"] == session_id

        assert len({path.name for path in user_one_paths}) == 4

        auth_state.update(uid="user-2", username="user_two")
        other_response = await client.post(
            "/api/v1/upload",
            data={"session_id": other_user_session.session_id},
            files={"file": ("shared.txt", b"user-2-session-one", "text/plain")},
        )
        assert other_response.status_code == 200
        other_payload = other_response.json()
        other_path = Path(other_payload["path"])
        assert other_path.parent == user_two_dir
        assert other_path.parent != user_one_dir
        assert other_path.read_bytes() == b"user-2-session-one"
        assert other_payload["filename"] == "shared.txt"
        assert other_payload["session_id"] == other_user_session.session_id

        other_user_download = await client.get(
            "/api/v1/download",
            params={"path": str(user_one_paths[0])},
        )
        assert other_user_download.status_code == 403
        assert other_user_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        forged_session_download = await client.get(
            "/api/v1/download",
            params={"path": str(user_one_paths[0]), "session_id": user_one_session.session_id},
        )
        assert forged_session_download.status_code == 403
        assert forged_session_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        download = await client.get(
            "/api/v1/download",
            params={"path": str(user_one_paths[0])},
        )
        assert download.status_code == 200
        assert download.content == b"user-1-session-one"
        assert download.headers["content-disposition"] == 'attachment; filename="shared.txt"'

        forged_owner_download = await client.get(
            "/api/v1/download",
            params={"path": str(other_path), "session_id": other_user_session.session_id},
        )
        assert forged_owner_download.status_code == 403
        assert forged_owner_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

    assert sorted(path.name for path in temp_root.iterdir()) == ["temp_user-1", "temp_user-2"]
    assert all(path.is_dir() for path in temp_root.iterdir())


@pytest.mark.asyncio
async def test_legacy_attachment_paths_are_not_downloadable_after_new_user_scoped_writes(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    app = _build_app(db_session, auth_state)

    image_buffer = io.BytesIO()
    image = Image.new("RGB", (2, 2), color=(32, 128, 224))
    try:
        image.save(image_buffer, format="PNG")
    finally:
        image.close()
    image_bytes = image_buffer.getvalue()
    legacy_text_bytes = b"legacy upload fixture"
    legacy_weixin_file_bytes = b"legacy weixin file fixture"

    legacy_session_dir = temp_root / "temp_legacy-session"
    legacy_generated_dir = legacy_session_dir / "generated_images"
    legacy_weixin_dir = temp_root / "weixin_openclaw"
    user_generated_dir = temp_root / "temp_user-1" / "generated_images"
    legacy_native_path = legacy_generated_dir / "generated_image_legacy-native.png"
    legacy_upload_path = legacy_session_dir / "12345678_legacy.txt"
    legacy_weixin_image_path = legacy_weixin_dir / "legacy_image.png"
    legacy_weixin_file_path = legacy_weixin_dir / "legacy_file.txt"
    legacy_tool_path = user_generated_dir / "generated_image_legacy-tool.png"
    unreferenced_dir = temp_root / "temp_unreferenced-session"
    unreferenced_path = unreferenced_dir / "87654321_unreferenced.txt"
    legacy_generated_dir.mkdir(parents=True)
    legacy_weixin_dir.mkdir(parents=True)
    user_generated_dir.mkdir(parents=True)
    unreferenced_dir.mkdir(parents=True)
    legacy_native_path.write_bytes(image_bytes)
    legacy_upload_path.write_bytes(legacy_text_bytes)
    legacy_weixin_image_path.write_bytes(image_bytes)
    legacy_weixin_file_path.write_bytes(legacy_weixin_file_bytes)
    legacy_tool_path.write_bytes(image_bytes)
    unreferenced_path.write_bytes(b"unreferenced legacy upload")

    db_session.add_all(
        [
            User(uid="user-1", username="user_one"),
            ChatSession(
                session_id="legacy-session",
                uid="user-1",
                source="http",
                reply_target_source="http",
            ),
            ChatSession(
                session_id="weixin-openclaw-session",
                uid="user-1",
                source="weixin-openclaw",
                reply_target_source="weixin-openclaw",
            ),
            Message(
                session_id="legacy-session",
                uid="user-1",
                role=MessageRole.USER,
                content="legacy attachment",
                profile_id=1,
                attachments=[str(legacy_upload_path)],
            ),
            Message(
                session_id="weixin-openclaw-session",
                uid="user-1",
                role=MessageRole.USER,
                content="legacy weixin attachments",
                profile_id=1,
                attachments=[str(legacy_weixin_image_path), str(legacy_weixin_file_path)],
            ),
        ]
    )
    await db_session.commit()

    legacy_native_token = send_file_to_user_module._encode_token({"path": str(legacy_native_path), "uid": "user-1", "id": "legacy-native-id"})
    legacy_tool_token = send_file_to_user_module._encode_token({"path": str(legacy_tool_path), "uid": "user-1", "id": "legacy-tool-id"})
    legacy_downloads = (
        (f"/api/v1/download-sent?token={quote(legacy_native_token)}", image_bytes, 200, None),
        (
            f"/api/v1/download?path={quote(str(legacy_upload_path), safe='')}",
            legacy_text_bytes,
            403,
            "legacy-session",
        ),
        (
            f"/api/v1/download?path={quote(str(legacy_weixin_image_path), safe='')}",
            image_bytes,
            403,
            "weixin-openclaw-session",
        ),
        (
            f"/api/v1/download?path={quote(str(legacy_weixin_file_path), safe='')}",
            legacy_weixin_file_bytes,
            403,
            "weixin-openclaw-session",
        ),
        (f"/api/v1/download-sent?token={quote(legacy_tool_token)}", image_bytes, 200, None),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:

        async def assert_legacy_downloads() -> None:
            for uid, username, is_superuser, include_session_id in (
                ("user-1", "user_one", False, False),
                ("user-1", "user_one", False, True),
                ("admin", "admin", True, False),
                ("admin", "admin", True, True),
            ):
                auth_state.update(uid=uid, username=username, is_superuser=is_superuser)
                for url, expected_bytes, expected_status, session_id in legacy_downloads:
                    request_url = httpx.URL(url)
                    if include_session_id and session_id:
                        request_url = request_url.copy_add_param("session_id", session_id)
                    response = await client.get(request_url)
                    assert response.status_code == expected_status
                    if expected_status == 200:
                        assert response.content == expected_bytes
                    else:
                        assert response.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        await assert_legacy_downloads()
        auth_state.update(uid="user-1", username="user_one", is_superuser=False)

        unreferenced_response = await client.get(
            "/api/v1/download",
            params={"path": str(unreferenced_path)},
        )
        assert unreferenced_response.status_code == 403
        assert unreferenced_response.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        upload_response = await client.post(
            "/api/v1/upload",
            files={"file": ("new-upload.txt", b"new upload content", "text/plain")},
        )
        assert upload_response.status_code == 200
        upload_path = Path(upload_response.json()["path"])
        assert upload_path.parent == get_user_temp_dir(tmp_path, "user-1")

        generated_files = await materialize_generated_images(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                generated_images=[
                    InternalGeneratedImage(
                        id="new-native-image-id",
                        data=base64.b64encode(image_bytes).decode("ascii"),
                        mime_type="image/png",
                    )
                ],
            ),
            project_root=tmp_path,
            uid="user-1",
            session_id="new-session",
            cfg=ProfileConfig(),
        )
        assert len(generated_files) == 1
        generated_path = send_file_to_user_module.resolve_file_token(generated_files[0]["id"])
        assert generated_path.parent == user_generated_dir.resolve()
        assert generated_path.read_bytes() == image_bytes
        assert not get_user_temp_dir(tmp_path, "new-session").exists()

        generated_download = await client.get(generated_files[0]["download_url"])
        assert generated_download.status_code == 200
        assert generated_download.content == image_bytes

        await assert_legacy_downloads()

    assert legacy_native_path.read_bytes() == image_bytes
    assert legacy_upload_path.read_bytes() == legacy_text_bytes
    assert legacy_weixin_image_path.read_bytes() == image_bytes
    assert legacy_weixin_file_path.read_bytes() == legacy_weixin_file_bytes
    assert legacy_tool_path.read_bytes() == image_bytes


@pytest.mark.asyncio
async def test_download_endpoints_require_authentication(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(security_module, "get_jwt_secret_key", lambda: "test-jwt-secret")

    sent_path = tmp_path / "anonymous-sent.txt"
    sent_path.write_bytes(b"sent file")
    token = send_file_to_user_module._encode_token({"path": str(sent_path), "uid": "user-1", "id": "anonymous-sent-id"})
    app = _build_unauthenticated_app(db_session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        upload_download = await client.get(
            "/api/v1/download",
            params={"path": str(sent_path)},
        )
        sent_download = await client.get(
            "/api/v1/download-sent",
            params={"token": token},
        )

    assert upload_download.status_code == 401
    assert upload_download.json()["message"] == t(ERR_UNAUTHORIZED)
    assert sent_download.status_code == 401
    assert sent_download.json()["message"] == t(ERR_UNAUTHORIZED)


@pytest.mark.asyncio
async def test_sent_download_allows_owner_and_superuser_rejects_other_user_and_tampering(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_path = tmp_path / "random-sent.txt"
    sent_path.write_bytes(b"random sent content")
    token = send_file_to_user_module._encode_token({"path": str(sent_path), "uid": "user-1", "id": "random-sent-id"})
    version, binding_key, secret = token.split(".")
    tampered_token = f"{version}.{'0' * len(binding_key)}.{secret}"

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner_download = await client.get("/api/v1/download-sent", params={"token": token})
        owner_range_download = await client.get(
            "/api/v1/download-sent",
            params={"token": token},
            headers={"Range": "bytes=0-5"},
        )
        auth_state.update(uid="user-2", username="user_two", is_superuser=False)
        other_user_download = await client.get("/api/v1/download-sent", params={"token": token})
        auth_state.update(uid="admin", username="admin", is_superuser=True)
        admin_download = await client.get("/api/v1/download-sent", params={"token": token})
        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        tampered_download = await client.get("/api/v1/download-sent", params={"token": tampered_token})

    assert owner_download.status_code == 200
    assert owner_download.content == b"random sent content"
    assert owner_range_download.status_code == 206
    assert owner_range_download.content == b"random"
    assert other_user_download.status_code == 403
    assert other_user_download.json()["message"] == t(ERR_UNAUTHORIZED)
    assert admin_download.status_code == 200
    assert admin_download.content == b"random sent content"
    assert tampered_download.status_code == 403
    assert tampered_download.json()["message"] == t(ERR_UNAUTHORIZED)


@pytest.mark.parametrize("headers", [{}, {"Range": "bytes=0-5"}])
@pytest.mark.asyncio
async def test_sent_download_reports_unavailable_if_source_is_removed_after_stat(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    source_path = tmp_path / "removed-after-stat.txt"
    source_path.write_bytes(b"source content")
    token = send_file_to_user_module._encode_token({"path": str(source_path), "uid": "user-1", "id": "removed-after-stat-id"})
    assert token.startswith("v2.")

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    original_open_file = anyio.open_file
    removed = False

    async def remove_source_before_open(path, mode="r", *args, **kwargs):
        nonlocal removed
        if not removed and Path(path) == source_path and mode == "rb":
            source_path.unlink()
            removed = True
        return await original_open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(anyio, "open_file", remove_source_before_open)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/download-sent", params={"token": token}, headers=headers)

    payload = response.json()
    assert removed
    assert response.status_code == 404
    assert payload["message"] == t(ERR_FILE_NOT_FOUND)
    assert payload["data"]["reason"] == "file_unavailable"


@pytest.mark.asyncio
async def test_temp_size_cleanup_removes_v2_tokens_for_deleted_files(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "temp"
    user_dir = temp_root / "temp_user-1"
    user_dir.mkdir(parents=True)
    deleted_path = user_dir / "old.txt"
    retained_path = user_dir / "new.txt"
    deleted_path.write_bytes(b"deleted file")
    retained_path.write_bytes(b"retained file")
    os.utime(deleted_path, (100.0, 100.0))
    os.utime(retained_path, (200.0, 200.0))

    missing_source_path = tmp_path / "outside-temp" / "missing.txt"
    missing_source_path.parent.mkdir()
    missing_source_path.write_bytes(b"missing source")

    monkeypatch.setattr(tasks_module, "TEMP_DIR", temp_root)
    deleted_tokens = (
        send_file_to_user_module._encode_token({"path": str(deleted_path), "uid": "user-1", "id": "deleted-owner-token"}),
        send_file_to_user_module._encode_token({"path": str(deleted_path), "uid": "user-2", "id": "deleted-other-token"}),
    )
    retained_token = send_file_to_user_module._encode_token({"path": str(retained_path), "uid": "user-1", "id": "retained-token"})
    missing_token = send_file_to_user_module._encode_token({"path": str(missing_source_path), "uid": "user-1", "id": "missing-source-token"})
    missing_source_path.unlink()

    token_records = {token: Path(send_file_to_user_module.FILE_TOKEN_DIR) / f"{token.split('.')[1]}.json" for token in (*deleted_tokens, retained_token, missing_token)}
    assert all(token.startswith("v2.") for token in token_records)
    assert all(record.exists() for record in token_records.values())

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        valid_download = await client.get("/api/v1/download-sent", params={"token": deleted_tokens[0]})
        assert valid_download.status_code == 200
        assert valid_download.content == b"deleted file"

        max_size_bytes = retained_path.stat().st_size
        deleted_count, current_size = tasks_module._cleanup_temp_dir_by_size(max_size_bytes)
        assert deleted_count == 1
        assert current_size == max_size_bytes
        assert not deleted_path.exists()
        assert retained_path.exists()
        assert all(not token_records[token].exists() for token in deleted_tokens)
        assert token_records[retained_token].exists()
        assert token_records[missing_token].exists()

        for uid, username, token in (
            ("user-1", "user_one", deleted_tokens[0]),
            ("user-2", "user_two", deleted_tokens[1]),
        ):
            auth_state.update(uid=uid, username=username, is_superuser=False)
            unavailable_download = await client.get("/api/v1/download-sent", params={"token": token})
            unavailable_payload = unavailable_download.json()
            assert unavailable_download.status_code == 404
            assert unavailable_payload["message"] == t(ERR_FILE_NOT_FOUND)
            assert unavailable_payload["data"]["reason"] == "file_unavailable"

        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        missing_download = await client.get("/api/v1/download-sent", params={"token": missing_token})
        missing_payload = missing_download.json()
        assert missing_download.status_code == 404
        assert missing_payload["message"] == t(ERR_FILE_NOT_FOUND)
        assert missing_payload["data"]["reason"] == "file_unavailable"

        retained_download = await client.get("/api/v1/download-sent", params={"token": retained_token})
        assert retained_download.status_code == 200
        assert retained_download.content == b"retained file"

        auth_state.update(uid="user-2", username="user_two", is_superuser=False)
        other_user_download = await client.get("/api/v1/download-sent", params={"token": retained_token})
        assert other_user_download.status_code == 403
        assert other_user_download.json()["message"] == t(ERR_UNAUTHORIZED)

        version, binding_key, secret = retained_token.split(".")
        replacement = "A" if secret[0] != "A" else "B"
        tampered_token = f"{version}.{binding_key}.{replacement}{secret[1:]}"
        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        tampered_download = await client.get("/api/v1/download-sent", params={"token": tampered_token})
        assert tampered_download.status_code == 403
        assert tampered_download.json()["message"] == t(ERR_UNAUTHORIZED)


@pytest.mark.asyncio
async def test_reconcile_file_download_tokens_cleans_stale_orphans_and_preserves_valid_records(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_uid = "active-user"
    disabled_uid = "disabled-user"
    deleted_uid = "deleted-user"
    deleted_user = User(uid=deleted_uid, username="deleted_user")
    db_session.add_all(
        [
            User(uid=active_uid, username="active_user"),
            User(uid=disabled_uid, username="disabled_user", is_active=False),
            deleted_user,
        ]
    )
    await db_session.commit()
    await db_session.delete(deleted_user)
    await db_session.commit()

    temp_root = tmp_path / "temp"
    audit_root = tmp_path / "audit"
    token_root = tmp_path / "file_download_tokens"
    monkeypatch.setattr(tasks_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(tasks_module, "AUDIT_DIR", audit_root)
    monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", token_root)

    stale_temp_path = temp_root / f"temp_{active_uid}" / "stale.txt"
    active_normal_path = temp_root / f"temp_{active_uid}" / "normal.txt"
    disabled_normal_path = audit_root / f"temp_{disabled_uid}" / "normal.json"
    deleted_external_path = tmp_path / "external" / "deleted.txt"
    missing_external_path = tmp_path / "external" / "temporarily-missing.txt"
    new_temp_path = temp_root / f"temp_{active_uid}" / "new.txt"
    source_paths = {
        stale_temp_path: b"stale temp source",
        active_normal_path: b"active normal source",
        disabled_normal_path: b"disabled normal source",
        deleted_external_path: b"deleted user source",
        missing_external_path: b"temporarily unavailable source",
        new_temp_path: b"new temp source",
    }
    for path, content in source_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def make_token(path: Path, uid: str, token_id: str) -> str:
        return send_file_to_user_module._encode_token({"path": str(path), "uid": uid, "id": token_id})

    tokens = {
        "stale_temp": make_token(stale_temp_path, active_uid, "stale-temp"),
        "deleted_external": make_token(deleted_external_path, deleted_uid, "deleted-external"),
        "active_normal": make_token(active_normal_path, active_uid, "active-normal"),
        "disabled_normal": make_token(disabled_normal_path, disabled_uid, "disabled-normal"),
        "missing_external": make_token(missing_external_path, active_uid, "missing-external"),
        "new_temp": make_token(new_temp_path, active_uid, "new-temp"),
    }
    token_records = {name: token_root / f"{token.split('.')[1]}.json" for name, token in tokens.items()}

    stale_token_names = (
        "stale_temp",
        "deleted_external",
        "active_normal",
        "disabled_normal",
        "missing_external",
    )
    for name in stale_token_names:
        os.utime(token_records[name], (1.0, 1.0))

    stale_temp_path.unlink()
    missing_external_path.unlink()
    new_temp_path.unlink()
    deleted_count = await tasks_module.reconcile_file_download_tokens(db_session)

    assert deleted_count == 2
    assert not token_records["stale_temp"].exists()
    assert not token_records["deleted_external"].exists()
    assert token_records["active_normal"].exists()
    assert token_records["disabled_normal"].exists()
    assert token_records["missing_external"].exists()
    assert token_records["new_temp"].exists()
    assert deleted_external_path.exists()
    assert not missing_external_path.exists()
    assert not new_temp_path.exists()


@pytest.mark.asyncio
async def test_superuser_upload_to_other_users_session_keeps_session_owner_directory(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_session = ChatSession(
        session_id="admin-upload-other-session",
        uid="user-2",
        source="http",
        reply_target_source="http",
    )
    db_session.add_all(
        [
            User(uid="user-1", username="user_one"),
            User(uid="user-2", username="user_two"),
            User(uid="admin", username="admin", is_superuser=True),
            other_session,
        ]
    )
    await db_session.commit()

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    auth_state: dict[str, str | bool] = {"uid": "admin", "username": "admin", "is_superuser": True}
    app = _build_app(db_session, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        upload_response = await client.post(
            "/api/v1/upload",
            data={"session_id": other_session.session_id},
            files={"file": ("admin-upload.txt", b"admin upload content", "text/plain")},
        )
        assert upload_response.status_code == 200
        upload_path = Path(upload_response.json()["path"])
        assert upload_path.parent == get_user_temp_dir(tmp_path, "user-2")
        assert upload_path.read_bytes() == b"admin upload content"

        admin_download = await client.get(
            "/api/v1/download",
            params={"path": str(upload_path), "session_id": other_session.session_id},
        )
        assert admin_download.status_code == 200
        assert admin_download.content == b"admin upload content"
        assert admin_download.headers["content-disposition"] == 'attachment; filename="admin-upload.txt"'

        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        forged_download = await client.get(
            "/api/v1/download",
            params={"path": str(upload_path), "session_id": other_session.session_id},
        )

    assert forged_download.status_code == 403
    assert forged_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)


@pytest.mark.asyncio
async def test_download_rejects_traversal_project_other_user_sensitive_directory_and_unreferenced_legacy_paths(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add_all(
        [
            User(uid="user-1", username="user_one"),
            User(uid="user-2", username="user_two"),
        ]
    )
    await db_session.commit()

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    user_one_dir = get_user_temp_dir(tmp_path, "user-1")
    user_two_dir = get_user_temp_dir(tmp_path, "user-2")
    user_one_dir.mkdir(parents=True)
    user_two_dir.mkdir(parents=True)

    project_file = tmp_path / "project-file.txt"
    project_file.write_bytes(b"project file")
    other_user_file = user_two_dir / "other.txt"
    other_user_file.write_bytes(b"other user file")
    sensitive_file = user_one_dir / ".env"
    sensitive_file.write_bytes(b"secret")
    directory_path = user_one_dir / "nested-directory"
    directory_path.mkdir()
    unreferenced_legacy_dir = temp_root / "temp_old-session"
    unreferenced_legacy_dir.mkdir(parents=True)
    unreferenced_legacy_file = unreferenced_legacy_dir / "old.txt"
    unreferenced_legacy_file.write_bytes(b"old file")
    traversal_path = user_one_dir / ".." / ".." / project_file.name

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    rejected_paths = (
        (project_file, 403, ERR_SESSION_NO_PERMISSION),
        (traversal_path, 403, ERR_SESSION_NO_PERMISSION),
        (other_user_file, 403, ERR_SESSION_NO_PERMISSION),
        (sensitive_file, 403, ERR_FILE_SENSITIVE_NOT_ALLOWED),
        (directory_path, 404, ERR_FILE_NOT_FOUND),
        (unreferenced_legacy_file, 403, ERR_SESSION_NO_PERMISSION),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for path, expected_status, expected_message in rejected_paths:
            response = await client.get("/api/v1/download", params={"path": str(path)})
            assert response.status_code == expected_status
            assert response.json()["message"] == t(expected_message)


@pytest.mark.asyncio
async def test_download_rejects_symlink_escape_without_touching_workspace_files(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    user_dir = get_user_temp_dir(tmp_path, "user-1")
    user_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"outside content")
    link_path = user_dir / "escaped.txt"
    try:
        link_path.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/download", params={"path": str(link_path)})

    assert response.status_code == 403
    assert response.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
    assert outside_file.read_bytes() == b"outside content"


@pytest.mark.asyncio
async def test_legacy_attachment_reference_cannot_cross_a_real_user_uid_collision(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    collision_dir = temp_root / "temp_user-2"
    collision_dir.mkdir(parents=True)
    collision_path = collision_dir / "12345678_collision.txt"
    collision_path.write_bytes(b"collision file")

    db_session.add_all(
        [
            User(uid="user-1", username="user_one"),
            User(uid="user-2", username="user_two"),
            ChatSession(
                session_id="legacy-collision-session",
                uid="user-1",
                source="http",
                reply_target_source="http",
            ),
            Message(
                session_id="legacy-collision-session",
                uid="user-1",
                role=MessageRole.USER,
                content="legacy collision attachment",
                profile_id=1,
                attachments=[str(collision_path)],
            ),
        ]
    )
    await db_session.commit()

    auth_state: dict[str, str | bool] = {"uid": "user-1", "username": "user_one", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/download", params={"path": str(collision_path)})

    assert response.status_code == 403
    assert response.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
    assert collision_path.read_bytes() == b"collision file"


@pytest.mark.parametrize(
    ("session_uid", "source", "error"),
    [
        ("user-2", "http", ERR_SESSION_NO_PERMISSION),
        ("user-1", "weixin-openclaw", ERR_SESSION_READ_ONLY),
    ],
)
@pytest.mark.asyncio
async def test_upload_rejects_non_writable_sessions(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_uid: str,
    source: str,
    error: str,
) -> None:
    session = ChatSession(
        session_id=f"upload-permission-{session_uid}-{source}",
        uid=session_uid,
        source=source,
        reply_target_source=source,
    )
    db_session.add(session)
    await db_session.commit()

    auth_state = {"uid": "user-1", "username": "user_one"}
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    app = _build_app(db_session, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/upload",
            data={"session_id": session.session_id},
            files={"file": ("rejected.txt", b"must not be written", "text/plain")},
        )

    assert response.status_code == 403
    assert response.json()["message"] == t(error)
    assert not temp_root.exists()


@pytest.mark.asyncio
async def test_weixin_inbound_attachments_are_user_scoped_and_downloadable(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = (
        ("system-owner-one", "owner_one", "wx-sender-one", "forged-owner-one"),
        ("system-owner-two", "owner_two", "wx-sender-two", "forged-owner-two"),
    )
    users = [User(uid=uid, username=username) for uid, username, _, _ in owners]
    users.append(User(uid="admin", username="admin", is_superuser=True))
    db_session.add_all(users)
    await db_session.commit()

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(weixin_media_module, "TEMP_DIR", temp_root)

    def make_png(color: tuple[int, int, int]) -> bytes:
        image_buffer = io.BytesIO()
        image = Image.new("RGB", (2, 2), color=color)
        try:
            image.save(image_buffer, format="PNG")
        finally:
            image.close()
        return image_buffer.getvalue()

    sessions: list[ChatSession] = []
    messages: list[Message] = []
    expected_by_owner: dict[str, list[tuple[str, bytes, str]]] = {}
    for owner_index, (owner_uid, _, sender_uid, forged_owner_uid) in enumerate(owners):
        platform = MessagePlatform(
            uid=owner_uid,
            config={"owner_uid": forged_owner_uid},
            name=f"weixin-{owner_uid}",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        )
        adapter = WeixinOpenClawPlatformHandler._build_adapter(platform)
        assert adapter.config.owner_uid == owner_uid

        direct_png = make_png((32 + owner_index, 96, 160))
        quoted_png = make_png((160, 96, 32 + owner_index))
        file_bytes = f"encrypted attachment for {owner_uid}".encode()
        image_key = bytes([owner_index + 1]) * 16
        file_key = bytes([owner_index + 3]) * 16
        direct_query = f"{sender_uid}/direct-image"
        quoted_query = f"{sender_uid}/quoted-image"
        file_query = f"{sender_uid}/document"
        downloaded_payloads = {
            direct_query: direct_png,
            quoted_query: aes_ecb_encrypt(pkcs7_pad(quoted_png), image_key),
            file_query: aes_ecb_encrypt(pkcs7_pad(file_bytes), file_key),
        }

        async def download_cdn_bytes(encrypted_query_param: str) -> bytes:
            return downloaded_payloads[encrypted_query_param]

        monkeypatch.setattr(adapter.client, "download_cdn_bytes", download_cdn_bytes)
        encrypted_image_item = {
            "type": IMAGE_ITEM_TYPE,
            "image_item": {
                "aeskey": image_key.hex(),
                "media": {"encrypt_query_param": quoted_query},
            },
        }
        inbound_messages = (
            (
                "direct-image",
                {"from_user_id": sender_uid, "item_list": [{"type": IMAGE_ITEM_TYPE, "image_item": {"media": {"encrypt_query_param": direct_query}}}]},
                direct_png,
                ".png",
            ),
            (
                "quoted-image",
                {
                    "from_user_id": sender_uid,
                    "item_list": [
                        {
                            "type": TEXT_ITEM_TYPE,
                            "text_item": {"text": "quoted image"},
                            "ref_msg": {"message_item": encrypted_image_item},
                        }
                    ],
                },
                quoted_png,
                ".png",
            ),
            (
                "encrypted-file",
                {
                    "from_user_id": sender_uid,
                    "item_list": [
                        {
                            "type": FILE_ITEM_TYPE,
                            "file_item": {
                                "file_name": "report.pdf",
                                "media": {
                                    "aes_key": base64.b64encode(file_key).decode("ascii"),
                                    "encrypt_query_param": file_query,
                                },
                            },
                        }
                    ],
                },
                file_bytes,
                ".pdf",
            ),
        )

        try:
            session_id = f"weixin-openclaw:{sender_uid}"
            owner_expectations: list[tuple[str, bytes, str]] = []
            for case_name, raw_message, expected_content, expected_suffix in inbound_messages:
                converted = await adapter.convert_message(raw_message)
                assert converted is not None
                assert converted.user_id == sender_uid
                assert converted.session_id == session_id
                assert len(converted.attachments) == 1
                owner_expectations.append((case_name, expected_content, expected_suffix))
                messages.append(
                    Message(
                        session_id=converted.session_id,
                        uid=owner_uid,
                        role=MessageRole.USER,
                        content=str(converted.text),
                        profile_id=1,
                        attachments=converted.attachments,
                    )
                )

            sessions.append(
                ChatSession(
                    session_id=session_id,
                    uid=owner_uid,
                    source="weixin-openclaw",
                    reply_target_source="weixin-openclaw",
                    message_platform_user_id=sender_uid,
                )
            )
            expected_by_owner[owner_uid] = owner_expectations
        finally:
            await adapter.close()

    db_session.add_all([*sessions, *messages])
    await db_session.commit()

    persisted_attachments: list[tuple[str, str, Path, bytes]] = []
    all_attachment_paths: list[Path] = []
    for owner_index, (owner_uid, _, sender_uid, forged_owner_uid) in enumerate(owners):
        session_id = f"weixin-openclaw:{sender_uid}"
        result = await db_session.execute(select(Message).where(Message.uid == owner_uid, Message.session_id == session_id).order_by(Message.id))
        persisted_messages = result.scalars().all()
        expectations = expected_by_owner[owner_uid]
        assert len(persisted_messages) == len(expectations) == 3
        for persisted_message, (_, expected_content, expected_suffix) in zip(persisted_messages, expectations, strict=True):
            assert persisted_message.attachments is not None
            assert len(persisted_message.attachments) == 1
            attachment_path = Path(persisted_message.attachments[0])
            assert attachment_path.parent == get_user_temp_dir(tmp_path, owner_uid)
            assert attachment_path.suffix == expected_suffix
            assert attachment_path.read_bytes() == expected_content
            all_attachment_paths.append(attachment_path)
            persisted_attachments.append((owner_uid, session_id, attachment_path, expected_content))

        assert session_id == sessions[owner_index].session_id
        assert not get_user_temp_dir(tmp_path, sender_uid).exists()
        assert not get_user_temp_dir(tmp_path, forged_owner_uid).exists()

    assert len({path.name for path in all_attachment_paths}) == len(all_attachment_paths) == 6
    assert sorted(path.name for path in temp_root.iterdir()) == sorted(f"temp_{uid}" for uid, _, _, _ in owners)
    assert all(path.is_dir() for path in temp_root.iterdir())

    username_by_uid = {uid: username for uid, username, _, _ in owners}
    auth_state: dict[str, str | bool] = {"uid": owners[0][0], "username": owners[0][1], "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for owner_uid, session_id, attachment_path, expected_content in persisted_attachments:
            for include_session_id in (False, True):
                auth_state.update(uid=owner_uid, username=username_by_uid[owner_uid], is_superuser=False)
                params = {"path": str(attachment_path)}
                if include_session_id:
                    params["session_id"] = session_id
                owner_download = await client.get("/api/v1/download", params=params)
                assert owner_download.status_code == 200
                assert owner_download.content == expected_content

            other_uid, other_username = next((uid, username) for uid, username, _, _ in owners if uid != owner_uid)
            auth_state.update(uid=other_uid, username=other_username, is_superuser=False)
            other_download = await client.get("/api/v1/download", params={"path": str(attachment_path)})
            assert other_download.status_code == 403
            assert other_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
            forged_session_download = await client.get(
                "/api/v1/download",
                params={"path": str(attachment_path), "session_id": session_id},
            )
            assert forged_session_download.status_code == 403
            assert forged_session_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

            auth_state.update(uid="admin", username="admin", is_superuser=True)
            admin_session_download = await client.get(
                "/api/v1/download",
                params={"path": str(attachment_path), "session_id": session_id},
            )
            assert admin_session_download.status_code == 200
            assert admin_session_download.content == expected_content
            admin_unscoped_download = await client.get("/api/v1/download", params={"path": str(attachment_path)})
            assert admin_unscoped_download.status_code == 403
            assert admin_unscoped_download.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
