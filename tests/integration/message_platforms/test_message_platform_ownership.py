import asyncio
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1 import message_platforms
from app.core.constants import ERR_MESSAGE_PLATFORM_NAME_EXISTS, ERR_USER_NOT_FOUND
from app.core.i18n import t
from app.models.message_platform import MessagePlatform, MessagePlatformType
from tests.integration.message_platforms import test_message_platform_permissions as permissions
from tests.integration.message_platforms.test_message_platform_permissions import (
    MESSAGE_PLATFORM_OPERATIONS,
    _assert_not_found,
    _assert_platforms_unchanged_except,
    _get_owned_profile_id,
    _platform,
    _platform_snapshot,
    _request,
    _request_for,
    _token,
)

api_context = permissions.api_context


@pytest.mark.asyncio
async def test_superuser_cannot_transfer_existing_message_platform_to_missing_uid(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
) -> None:
    app, session_factory, platform_ids, calls = api_context
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_ids["ordinary"]},
            headers={"Authorization": f"Bearer {_token('admin')}"},
            json={
                "name": "missing-user-message-platform",
                "uid": "missing-user",
                "profile_id": None,
                "is_enabled": False,
            },
        )

    payload = response.json()
    assert response.status_code == 404
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_USER_NOT_FOUND)
    assert payload["data"] is None
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
async def test_superuser_can_transfer_unbind_and_delete_message_platform(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
) -> None:
    app, session_factory, platform_ids, calls = api_context
    admin_token = _token("admin")
    ordinary_token = _token("ordinary")
    other_token = _token("other")
    ordinary_profile_id = await _get_owned_profile_id(session_factory, "ordinary")
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    created_token = "admin-transfer-platform-token"
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "admin-transfer-message-platform",
                "uid": "other",
                "profile_id": other_profile_id,
                "is_enabled": True,
                "config": {"token": created_token},
            },
        )
        create_payload = create_response.json()
        assert create_response.status_code == 200
        assert create_payload["code"] == 200
        created = create_payload["data"]
        created_id = created["id"]
        assert created["uid"] == "other"
        assert created["profile_id"] == other_profile_id
        assert created["config"]["token"] == created_token

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid == "other"
            assert persisted.profile_id == other_profile_id
            assert persisted.get_config_secret("token") == created_token

        after_create = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_create, created_id)
        assert calls == []

        ordinary_denied = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_denied)
        assert created_token not in ordinary_denied.text
        assert await _platform_snapshot(session_factory) == after_create

        other_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        other_get_payload = other_get.json()
        assert other_get.status_code == 200
        assert other_get_payload["code"] == 200
        other_data = other_get_payload["data"]
        assert other_data["id"] == created_id
        assert other_data["uid"] == "other"
        assert other_data["profile_id"] == other_profile_id
        assert other_data["config"]["token"] == created_token
        assert await _platform_snapshot(session_factory) == after_create

        transfer_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"uid": "ordinary", "profile_id": ordinary_profile_id},
        )
        transfer_payload = transfer_response.json()
        assert transfer_response.status_code == 200
        assert transfer_payload["code"] == 200
        transferred = transfer_payload["data"]
        assert transferred["uid"] == "ordinary"
        assert transferred["profile_id"] == ordinary_profile_id

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid == "ordinary"
            assert persisted.profile_id == ordinary_profile_id
            assert persisted.get_config_secret("token") == created_token

        after_transfer = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_transfer, created_id)

        for operation in MESSAGE_PLATFORM_OPERATIONS:
            denied = await _request(client, _request_for(operation, created_id), other_token)
            _assert_not_found(denied)
            assert created_token not in denied.text
            assert await _platform_snapshot(session_factory) == after_transfer
        assert calls == []

        ordinary_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        ordinary_get_payload = ordinary_get.json()
        assert ordinary_get.status_code == 200
        assert ordinary_get_payload["code"] == 200
        ordinary_data = ordinary_get_payload["data"]
        assert ordinary_data["uid"] == "ordinary"
        assert ordinary_data["profile_id"] == ordinary_profile_id
        assert ordinary_data["config"]["token"] == created_token
        assert await _platform_snapshot(session_factory) == after_transfer

        unbind_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"uid": None, "profile_id": None, "is_enabled": False},
        )
        unbind_payload = unbind_response.json()
        assert unbind_response.status_code == 200
        assert unbind_payload["code"] == 200
        unbound = unbind_payload["data"]
        assert unbound["uid"] is None
        assert unbound["profile_id"] is None
        assert unbound["is_enabled"] is False

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid is None
            assert persisted.profile_id is None
            assert persisted.is_enabled is False

        after_unbind = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_unbind, created_id)

        ordinary_unbound_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_unbound_get)
        assert created_token not in ordinary_unbound_get.text

        ordinary_unbound_delete = await client.post(
            "/api/v1/message-platforms/delete",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_unbound_delete)
        assert created_token not in ordinary_unbound_delete.text

        ordinary_list = await client.get(
            "/api/v1/message-platforms/list",
            params={"page": 1, "size": 100},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        ordinary_list_payload = ordinary_list.json()
        assert ordinary_list.status_code == 200
        assert ordinary_list_payload["code"] == 200
        ordinary_items = ordinary_list_payload["data"]["items"]
        assert ordinary_list_payload["data"]["total"] == 1
        assert created_id not in {item["id"] for item in ordinary_items}
        assert created_token not in ordinary_list.text

        admin_unbound_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        admin_unbound_payload = admin_unbound_get.json()
        assert admin_unbound_get.status_code == 200
        assert admin_unbound_payload["code"] == 200
        admin_unbound = admin_unbound_payload["data"]
        assert admin_unbound["uid"] is None
        assert admin_unbound["profile_id"] is None
        assert admin_unbound["config"]["token"] == created_token

        admin_delete = await client.post(
            "/api/v1/message-platforms/delete",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        admin_delete_payload = admin_delete.json()
        assert admin_delete.status_code == 200
        assert admin_delete_payload["code"] == 200
        assert admin_delete_payload["data"] is None

    async with session_factory() as session:
        assert await session.get(MessagePlatform, created_id) is None
    after_delete = await _platform_snapshot(session_factory)
    assert after_delete == before
    assert len(after_delete) == len(before)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("login-start", "login-status"))
async def test_old_owner_cannot_write_platform_after_transfer_during_login_external_wait(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    admin_token = _token("admin")
    ordinary_token = _token("ordinary")
    transfer_count = 0
    transfer_snapshots: list[tuple[tuple[Any, ...], ...]] = []
    external_calls: list[str] = []
    new_token = "race-login-token"
    new_account_id = "race-login-account"
    new_qrcode = "race-login-qrcode"
    new_qrcode_image = "race-login-qrcode-image"
    before = await _platform_snapshot(session_factory)

    async def transfer_platform_owner() -> None:
        nonlocal transfer_count
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin_client:
            admin_response = await admin_client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": platform_id},
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"uid": "other", "profile_id": other_profile_id},
            )
        admin_payload = admin_response.json()
        assert admin_response.status_code == 200
        assert admin_payload["code"] == 200
        assert admin_payload["data"]["uid"] == "other"
        assert admin_payload["data"]["profile_id"] == other_profile_id
        transfer_count += 1
        transfer_snapshots.append(await _platform_snapshot(session_factory))

    async def race_start_login_session(_adapter) -> dict[str, Any]:
        external_calls.append("start_login_session")
        await transfer_platform_owner()
        return {
            "qrcode": new_qrcode,
            "qrcode_img_content": new_qrcode_image,
            "qrcode_status": "wait",
            "started_at": 1.0,
        }

    async def race_poll_qrcode_status(_adapter, qrcode: str) -> dict[str, Any]:
        external_calls.append(f"poll_qrcode_status:{qrcode}")
        await transfer_platform_owner()
        return {
            "qrcode_status": "confirmed",
            "token": new_token,
            "account_id": new_account_id,
            "base_url": "https://weixin.example",
            "user_id": "",
        }

    if operation == "login-start":
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "start_login_session", race_start_login_session)
    else:
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "poll_qrcode_status", race_poll_qrcode_status)

    request = _request_for(operation, platform_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _request(client, request, ordinary_token)

    _assert_not_found(response)
    assert response.status_code == 404
    for sensitive_value in (new_token, new_account_id, new_qrcode, new_qrcode_image):
        assert sensitive_value not in response.text
    assert transfer_count == 1
    assert external_calls == (["start_login_session"] if operation == "login-start" else ["poll_qrcode_status:ordinary-platform-qrcode"])
    assert len(transfer_snapshots) == 1
    transfer_snapshot = transfer_snapshots[0]
    _assert_platforms_unchanged_except(before, transfer_snapshot, platform_id)
    transferred_row = next(row for row in transfer_snapshot if row[0] == platform_id)
    assert transferred_row[8] == "other"
    assert transferred_row[9] == other_profile_id
    assert await _platform_snapshot(session_factory) == transfer_snapshot
    assert calls == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("other", "admin"))
async def test_distinct_users_can_create_same_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    actor: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    name = "ordinary-message-platform"
    new_token = f"{actor}-same-name-platform-token"
    request_payload: dict[str, Any] = {
        "name": name,
        "is_enabled": True,
        "config": {"token": new_token},
    }
    if actor == "admin":
        request_payload["uid"] = " other "

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            json=request_payload,
        )
        create_payload = create_response.json()
        assert create_response.status_code == 200
        assert create_payload["code"] == 200
        created = create_payload["data"]
        created_id = created["id"]
        assert created["uid"] == "other"
        assert created_id != platform_ids["ordinary"]
        assert created["config"]["token"] == new_token

        ordinary_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {_token('ordinary')}"},
        )
        _assert_not_found(ordinary_get)
        assert new_token not in ordinary_get.text

    async with session_factory() as session:
        same_name = (await session.execute(select(MessagePlatform).where(MessagePlatform.name == name).order_by(MessagePlatform.id))).scalars().all()
        assert len(same_name) == 2
        by_uid = {platform.uid: platform for platform in same_name}
        assert set(by_uid) == {"ordinary", "other"}
        assert by_uid["ordinary"].id == platform_ids["ordinary"]
        assert by_uid["ordinary"].get_config_secret("token") == "ordinary-platform-token"
        assert by_uid["other"].id == created_id
        assert by_uid["other"].get_config_secret("token") == new_token

    after = await _platform_snapshot(session_factory)
    _assert_platforms_unchanged_except(before, after, created_id)
    assert len(after) == len(before) + 1
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("ordinary", "admin"))
async def test_same_user_cannot_create_duplicate_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    actor: str,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    new_token = f"{actor}-duplicate-platform-token"
    request_payload: dict[str, Any] = {
        "name": "ordinary-message-platform",
        "is_enabled": True,
        "config": {"token": new_token},
    }
    if actor == "admin":
        request_payload["uid"] = " ordinary "

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            json=request_payload,
        )

    payload = response.json()
    assert response.status_code == 400
    assert payload["code"] == 400
    assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    assert payload["data"] is None
    assert new_token not in response.text
    assert "ordinary-platform-token" not in response.text
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name_case", "expected_status"),
    (("cross-owner", 200), ("same-owner", 400), ("unchanged", 200)),
)
async def test_ordinary_user_update_name_checks_owner_scoped_uniqueness(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    name_case: str,
    expected_status: int,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    target_name = {
        "cross-owner": "other-message-platform",
        "same-owner": "ordinary-second-message-platform",
        "unchanged": "ordinary-message-platform",
    }[name_case]

    async with session_factory() as session:
        second_ordinary = _platform(
            name="ordinary-second-message-platform",
            uid="ordinary",
            profile_id=None,
            token="ordinary-second-platform-token",
            qrcode="ordinary-second-platform-qrcode",
            account_id="ordinary-second-platform-account",
            last_error="ordinary-second-platform-error",
        )
        session.add(second_ordinary)
        await session.commit()
        assert second_ordinary.id is not None
        assert second_ordinary.platform_type == MessagePlatformType.WEIXIN_OPENCLAW

    before = await _platform_snapshot(session_factory)
    token = _token("ordinary")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={"name": target_name, "language": "en"},
        )

    payload = response.json()
    assert response.status_code == expected_status
    if expected_status == 200:
        assert payload["code"] == 200
        updated = payload["data"]
        assert updated["name"] == target_name
        assert updated["language"] == "en"
        assert updated["uid"] == "ordinary"

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.name == target_name
            assert persisted.language == "en"
            assert persisted.uid == "ordinary"

        after = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after, platform_id)
    else:
        assert set(payload) == {"code", "message", "data"}
        assert payload["code"] == 400
        assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
        assert payload["data"] is None
        assert await _platform_snapshot(session_factory) == before

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("owner-conflict", "renamed-conflict", "owner-free", "renamed-free"))
async def test_superuser_transfer_checks_name_uniqueness_by_final_owner_and_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    case: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    target_name = {
        "owner-conflict": "ordinary-message-platform",
        "renamed-conflict": "other-message-platform",
        "owner-free": "ordinary-message-platform",
        "renamed-free": "ordinary-local-platform",
    }[case]

    if case == "owner-conflict":
        async with session_factory() as session:
            session.add(
                MessagePlatform(
                    platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                    uid="other",
                    name="ordinary-message-platform",
                )
            )
            await session.commit()
    elif case == "renamed-free":
        async with session_factory() as session:
            session.add(
                MessagePlatform(
                    platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                    uid="ordinary",
                    name="ordinary-local-platform",
                )
            )
            await session.commit()

    request_payload: dict[str, Any] = {"uid": " other ", "profile_id": other_profile_id}
    if case in ("renamed-conflict", "renamed-free"):
        request_payload["name"] = target_name

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {_token('admin')}"},
            json=request_payload,
        )
        payload = response.json()

        if case in ("owner-conflict", "renamed-conflict"):
            assert response.status_code == 400
            assert set(payload) == {"code", "message", "data"}
            assert payload["code"] == 400
            assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
            assert payload["data"] is None
            assert await _platform_snapshot(session_factory) == before
        else:
            assert response.status_code == 200
            assert payload["code"] == 200
            updated = payload["data"]
            assert updated["uid"] == "other"
            assert updated["name"] == target_name
            assert updated["profile_id"] == other_profile_id

            async with session_factory() as session:
                persisted = await session.get(MessagePlatform, platform_id)
                assert persisted is not None
                assert persisted.uid == "other"
                assert persisted.name == target_name
                assert persisted.profile_id == other_profile_id

            after = await _platform_snapshot(session_factory)
            _assert_platforms_unchanged_except(before, after, platform_id)

            old_owner_get = await client.get(
                "/api/v1/message-platforms/get",
                params={"platform_id": platform_id},
                headers={"Authorization": f"Bearer {_token('ordinary')}"},
            )
            _assert_not_found(old_owner_get)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("second_actor", ("ordinary", "other"))
async def test_concurrent_users_create_same_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    second_actor: str,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    name = "concurrent-name-platform"
    request_tokens = ("concurrent-token-0", "concurrent-token-1")
    actors = ("ordinary", second_actor)
    barrier = asyncio.Barrier(2)

    async def authenticate_with_barrier(token: str = Depends(security.oauth2_scheme)):
        user = await security.get_current_user(token)
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return user

    app.dependency_overrides[security.get_current_user] = authenticate_with_barrier
    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.wait_for(
            asyncio.gather(
                client.post(
                    "/api/v1/message-platforms/create",
                    headers={"Authorization": f"Bearer {_token(actors[0])}"},
                    json={"name": name, "is_enabled": True, "config": {"token": request_tokens[0]}},
                ),
                client.post(
                    "/api/v1/message-platforms/create",
                    headers={"Authorization": f"Bearer {_token(actors[1])}"},
                    json={"name": name, "is_enabled": True, "config": {"token": request_tokens[1]}},
                ),
            ),
            timeout=30,
        )

    assert sorted(response.status_code for response in responses) == ([200, 400] if second_actor == "ordinary" else [200, 200])
    successful = []
    for index, response in enumerate(responses):
        payload = response.json()
        if response.status_code == 200:
            assert payload["code"] == 200
            created = payload["data"]
            assert created["uid"] == actors[index]
            assert created["name"] == name
            assert created["config"]["token"] == request_tokens[index]
            successful.append((index, created))
        else:
            assert response.status_code == 400
            assert set(payload) == {"code", "message", "data"}
            assert payload["code"] == 400
            assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
            assert payload["data"] is None
            assert request_tokens[0] not in response.text
            assert request_tokens[1] not in response.text

    async with session_factory() as session:
        same_name = (await session.execute(select(MessagePlatform).where(MessagePlatform.name == name).order_by(MessagePlatform.id))).scalars().all()
        assert len(same_name) == len(successful)
        persisted_by_id = {platform.id: platform for platform in same_name}
        assert set(persisted_by_id) == {created["id"] for _, created in successful}
        for index, created in successful:
            persisted = persisted_by_id[created["id"]]
            assert persisted.uid == actors[index]
            assert persisted.name == name
            assert persisted.get_config_secret("token") == request_tokens[index]

    after = await _platform_snapshot(session_factory)
    before_ids = {row[0] for row in before}
    assert tuple(row for row in after if row[0] in before_ids) == before
    assert len(after) == len(before) + len(successful)
    persisted_tokens = {platform.get_config_secret("token") for platform in same_name}
    assert persisted_tokens == {request_tokens[index] for index, _ in successful}
    assert calls == []
