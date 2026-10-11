import base64
import io
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.security as security_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1 import files as files_module
from app.core.constants import (
    ERR_FILE_NOT_FOUND,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_READ_ONLY,
    ERR_UNAUTHORIZED,
)
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.utils.assistant_files import materialize_generated_images
from app.handler import register_handlers
from app.models.message import InternalGeneratedImage, InternalMessage, Message, MessageRole
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
async def test_legacy_file_paths_remain_downloadable_after_new_user_scoped_writes(
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

    legacy_session_dir = temp_root / "temp_legacy-session"
    legacy_generated_dir = legacy_session_dir / "generated_images"
    user_generated_dir = temp_root / "temp_user-1" / "generated_images"
    legacy_native_path = legacy_generated_dir / "generated_image_legacy-native.png"
    legacy_upload_path = legacy_session_dir / "12345678_legacy.txt"
    legacy_tool_path = user_generated_dir / "generated_image_legacy-tool.png"
    unreferenced_dir = temp_root / "temp_unreferenced-session"
    unreferenced_path = unreferenced_dir / "87654321_unreferenced.txt"
    legacy_generated_dir.mkdir(parents=True)
    user_generated_dir.mkdir(parents=True)
    unreferenced_dir.mkdir(parents=True)
    legacy_native_path.write_bytes(image_bytes)
    legacy_upload_path.write_bytes(legacy_text_bytes)
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
            Message(
                session_id="legacy-session",
                uid="user-1",
                role=MessageRole.USER,
                content="legacy attachment",
                profile_id=1,
                attachments=[str(legacy_upload_path)],
            ),
        ]
    )
    await db_session.commit()

    legacy_native_token = send_file_to_user_module._encode_token({"path": str(legacy_native_path), "uid": "user-1", "id": "legacy-native-id"})
    legacy_tool_token = send_file_to_user_module._encode_token({"path": str(legacy_tool_path), "uid": "user-1", "id": "legacy-tool-id"})
    legacy_downloads = (
        (f"/api/v1/download-sent?token={quote(legacy_native_token)}", image_bytes),
        (f"/api/v1/download?path={quote(str(legacy_upload_path), safe='')}", legacy_text_bytes),
        (f"/api/v1/download-sent?token={quote(legacy_tool_token)}", image_bytes),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for url, expected_bytes in legacy_downloads:
            response = await client.get(url)
            assert response.status_code == 200
            assert response.content == expected_bytes

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

        for url, expected_bytes in legacy_downloads:
            response = await client.get(url)
            assert response.status_code == 200
            assert response.content == expected_bytes

    assert all(path.is_file() for path in (legacy_native_path, legacy_upload_path, legacy_tool_path))


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
        auth_state.update(uid="user-2", username="user_two", is_superuser=False)
        other_user_download = await client.get("/api/v1/download-sent", params={"token": token})
        auth_state.update(uid="admin", username="admin", is_superuser=True)
        admin_download = await client.get("/api/v1/download-sent", params={"token": token})
        auth_state.update(uid="user-1", username="user_one", is_superuser=False)
        tampered_download = await client.get("/api/v1/download-sent", params={"token": tampered_token})

    assert owner_download.status_code == 200
    assert owner_download.content == b"random sent content"
    assert other_user_download.status_code == 403
    assert other_user_download.json()["message"] == t(ERR_UNAUTHORIZED)
    assert admin_download.status_code == 200
    assert admin_download.content == b"random sent content"
    assert tampered_download.status_code == 403
    assert tampered_download.json()["message"] == t(ERR_UNAUTHORIZED)


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
