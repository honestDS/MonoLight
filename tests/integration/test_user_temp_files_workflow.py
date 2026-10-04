import base64
import io
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1 import files as files_module
from app.core.constants import ERR_SESSION_NO_PERMISSION, ERR_SESSION_READ_ONLY
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.utils.assistant_files import materialize_generated_images
from app.handler import register_handlers
from app.models.message import InternalGeneratedImage, InternalMessage, MessageRole
from app.models.profile import ProfileConfig
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db


def _build_app(db_session: AsyncSession, auth_state: dict[str, str]) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(files_module.router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user() -> User:
        return User(uid=auth_state["uid"], username=auth_state["username"])

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
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
    db_session.add_all([user_one_session, user_two_session, other_user_session])
    await db_session.commit()

    auth_state = {"uid": "user-1", "username": "user_one"}
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

        download = await client.get(
            "/api/v1/download",
            params={"path": str(user_one_paths[0])},
        )
        assert download.status_code == 200
        assert download.content == b"user-1-session-one"
        assert download.headers["content-disposition"] == 'attachment; filename="shared.txt"'

    assert sorted(path.name for path in temp_root.iterdir()) == ["temp_user-1", "temp_user-2"]
    assert all(path.is_dir() for path in temp_root.iterdir())


@pytest.mark.asyncio
async def test_legacy_file_paths_remain_downloadable_after_new_user_scoped_writes(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_state = {"uid": "user-1", "username": "user_one"}
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"f" * 32)
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
    legacy_generated_dir.mkdir(parents=True)
    user_generated_dir.mkdir(parents=True)
    legacy_native_path.write_bytes(image_bytes)
    legacy_upload_path.write_bytes(legacy_text_bytes)
    legacy_tool_path.write_bytes(image_bytes)

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
