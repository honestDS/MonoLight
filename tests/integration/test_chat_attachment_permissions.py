import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.utils.file_access as file_access
from app.core.constants import ERR_SESSION_NO_PERMISSION
from app.core.i18n import t
from app.models.message import Message, MessageRole
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.user import User
from tests.integration.test_chat_session_workflow import (
    _build_app,
    _patch_http_submission,
    _seed_profiles,
    chat_session_database,
)

__all__ = ["chat_session_database"]


async def _session_ids(db: AsyncSession, session_id: str) -> tuple[list[int], list[int]]:
    messages = list((await db.execute(select(Message.id).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
    works = list((await db.execute(select(SessionReplyWorkItem.id).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
    return messages, works


async def _assert_submission(
    db: AsyncSession,
    response: httpx.Response,
    *,
    request_id: str,
    session_id: str,
    uid: str,
    expected_attachments: list[str] | None = None,
) -> Message:
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["session_id"] == session_id
    assert data["request_id"] == request_id
    assert data["submission_status"] in {"accepted", "queued"}

    work = await db.get(SessionReplyWorkItem, data["work_id"])
    assert work is not None
    assert work.uid == uid
    assert work.session_id == session_id
    assert work.source_id is not None

    message = await db.get(Message, int(work.source_id))
    assert message is not None
    assert message.uid == uid
    assert message.session_id == session_id
    assert message.attachments == expected_attachments
    return message


def _assert_forbidden(response: httpx.Response) -> None:
    assert response.status_code == 403
    payload = response.json()
    assert payload["code"] == 403
    assert payload["message"] == t(ERR_SESSION_NO_PERMISSION)


@pytest.mark.asyncio
async def test_http_chat_file_inputs_stay_bound_to_session_owner(
    chat_session_database: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None

    session_id = "attachment-permission-session"
    old_session_id = "attachment-permission-old-session"
    unreferenced_session_id = "attachment-permission-unreferenced"
    target_session = ChatSession(
        session_id=session_id,
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    old_session = ChatSession(
        session_id=old_session_id,
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    unreferenced_session = ChatSession(
        session_id=unreferenced_session_id,
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add_all(
        [
            User(uid="user-2", username="otheruser"),
            User(uid="admin", username="admin"),
            target_session,
            old_session,
            unreferenced_session,
        ]
    )
    await chat_session_database.commit()

    temp_root = tmp_path / "temp"
    owner_dir = temp_root / "temp_user-1"
    other_dir = temp_root / "temp_user-2"
    admin_dir = temp_root / "temp_admin"
    old_dir = temp_root / "temp_attachment-permission-old-session"
    unreferenced_dir = temp_root / "temp_attachment-permission-unreferenced"
    shared_dir = temp_root / "weixin_openclaw"
    owner_file = owner_dir / "owner.txt"
    other_file = other_dir / "other.txt"
    admin_file = admin_dir / "admin.txt"
    old_file = old_dir / "old.txt"
    unreferenced_file = unreferenced_dir / "unreferenced.txt"
    shared_file = shared_dir / "shared.txt"
    outside_file = tmp_path / "outside-project.txt"
    for path in (
        owner_file,
        other_file,
        admin_file,
        old_file,
        unreferenced_file,
        shared_file,
        outside_file,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    chat_session_database.add(
        Message(
            session_id=old_session_id,
            uid="user-1",
            role=MessageRole.USER,
            content="historical attachment",
            profile_id=primary_profile.id,
            attachments=[str(old_file), str(shared_file)],
        )
    )
    await chat_session_database.commit()

    monkeypatch.setattr(file_access, "TEMP_DIR", temp_root)
    _patch_http_submission(monkeypatch, primary_profile)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        request_id = "attachment-owner-request"
        owner_attachment = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "owner attachment",
                "attachments": [str(owner_file)],
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        await _assert_submission(
            chat_session_database,
            owner_attachment,
            request_id=request_id,
            session_id=session_id,
            uid="user-1",
            expected_attachments=[str(owner_file)],
        )

        invalid_requests = [
            {
                "message": "other attachment",
                "attachments": [str(other_file)],
            },
            {
                "message": "outside attachment",
                "attachments": [str(outside_file)],
            },
            {
                "message": [{"type": "file", "path": str(other_file)}],
            },
            {
                "message": [{"type": "audio", "format": "wav", "path": str(other_file)}],
            },
            {
                "message": [{"type": "file", "path": str(outside_file)}],
            },
            {
                "message": [{"type": "audio", "format": "wav", "path": str(outside_file)}],
            },
        ]
        for index, invalid_request in enumerate(invalid_requests, start=1):
            before = await _session_ids(chat_session_database, session_id)
            response = await client.post(
                "/api/v1/chat/completions",
                json={
                    **invalid_request,
                    "session_id": session_id,
                    "request_id": f"invalid-attachment-request-{index}",
                },
            )
            _assert_forbidden(response)
            assert await _session_ids(chat_session_database, session_id) == before

        file_request_id = "structured-file-owner-request"
        file_parts = [{"type": "file", "path": str(owner_file)}]
        file_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": file_parts,
                "session_id": session_id,
                "request_id": file_request_id,
            },
        )
        file_message = await _assert_submission(
            chat_session_database,
            file_response,
            request_id=file_request_id,
            session_id=session_id,
            uid="user-1",
        )
        assert json.loads(file_message.content or "[]")[0]["path"] == str(owner_file)

        audio_request_id = "structured-audio-owner-request"
        audio_parts = [{"type": "audio", "format": "wav", "path": str(owner_file)}]
        audio_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": audio_parts,
                "session_id": session_id,
                "request_id": audio_request_id,
            },
        )
        audio_message = await _assert_submission(
            chat_session_database,
            audio_response,
            request_id=audio_request_id,
            session_id=session_id,
            uid="user-1",
        )
        assert json.loads(audio_message.content or "[]")[0]["path"] == str(owner_file)

        data_only_request_id = "structured-audio-data-only-request"
        data_only_audio = [{"type": "audio", "format": "wav", "data": "AQID"}]
        data_only_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": data_only_audio,
                "session_id": session_id,
                "request_id": data_only_request_id,
            },
        )
        data_only_message = await _assert_submission(
            chat_session_database,
            data_only_response,
            request_id=data_only_request_id,
            session_id=session_id,
            uid="user-1",
        )
        assert json.loads(data_only_message.content or "[]")[0]["data"] == "AQID"

        auth_state.update(uid="admin", is_superuser=True)
        admin_owner_request_id = "admin-owner-attachment-request"
        admin_owner_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "admin submitting for owner",
                "attachments": [str(owner_file)],
                "session_id": session_id,
                "request_id": admin_owner_request_id,
            },
        )
        await _assert_submission(
            chat_session_database,
            admin_owner_response,
            request_id=admin_owner_request_id,
            session_id=session_id,
            uid="user-1",
            expected_attachments=[str(owner_file)],
        )

        before_admin_path = await _session_ids(chat_session_database, session_id)
        admin_path_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "admin path must not impersonate owner",
                "attachments": [str(admin_file)],
                "session_id": session_id,
                "request_id": "admin-own-directory-request",
            },
        )
        _assert_forbidden(admin_path_response)
        assert await _session_ids(chat_session_database, session_id) == before_admin_path

        for role, uid, is_superuser in (
            ("owner", "user-1", False),
            ("admin", "admin", True),
        ):
            auth_state.update(uid=uid, is_superuser=is_superuser)
            for directory, path in (("old", old_file), ("shared", shared_file)):
                invalid_requests = (
                    (
                        "attachment",
                        {
                            "message": f"{role} {directory} attachment",
                            "attachments": [str(path)],
                        },
                    ),
                    (
                        "structured-file",
                        {"message": [{"type": "file", "path": str(path)}]},
                    ),
                    (
                        "structured-audio",
                        {"message": [{"type": "audio", "format": "wav", "path": str(path)}]},
                    ),
                )
                for input_type, invalid_request in invalid_requests:
                    before = await _session_ids(chat_session_database, session_id)
                    response = await client.post(
                        "/api/v1/chat/completions",
                        json={
                            **invalid_request,
                            "session_id": session_id,
                            "request_id": f"{role}-{directory}-{input_type}-request",
                        },
                    )
                    _assert_forbidden(response)
                    assert await _session_ids(chat_session_database, session_id) == before

        auth_state.update(uid="user-1", is_superuser=False)
        before_unreferenced = await _session_ids(chat_session_database, session_id)
        unreferenced_response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "unreferenced historical attachment",
                "attachments": [str(unreferenced_file)],
                "session_id": session_id,
                "request_id": "unreferenced-historical-attachment-request",
            },
        )
        _assert_forbidden(unreferenced_response)
        assert await _session_ids(chat_session_database, session_id) == before_unreferenced
