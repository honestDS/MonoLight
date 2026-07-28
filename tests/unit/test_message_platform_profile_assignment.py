from types import SimpleNamespace

import pytest

from app.api.v1 import message_platforms as message_platforms_api
from app.models.message_platform import MessagePlatformCreate, MessagePlatformType, MessagePlatformUpdate


class FakeDb:
    pass


def _patch_response_serialization(monkeypatch):
    monkeypatch.setattr(
        message_platforms_api.MessagePlatformResponse,
        "model_validate",
        classmethod(lambda cls, platform, **kwargs: {"id": platform.id}),
    )


@pytest.mark.asyncio
async def test_create_message_platform_validates_profile_against_final_uid(monkeypatch):
    db = FakeDb()
    calls = []
    created_payloads = []

    def normalize_create(payload):
        calls.append(("normalize", payload.name))
        return {
            "name": payload.name,
            "platform_type": payload.platform_type,
            "is_enabled": payload.is_enabled,
            "uid": payload.uid,
            "profile_id": payload.profile_id,
            "config": {},
            "state": {},
        }

    async def get_by_name(db_arg, name):
        assert db_arg is db
        calls.append(("get_by_name", name))
        return None

    async def validate_profile(db_arg, *, profile_id, uid):
        assert db_arg is db
        calls.append(("validate", profile_id, uid))
        return SimpleNamespace(id=profile_id, uid=uid)

    async def create(db_arg, *, obj_in):
        assert db_arg is db
        created_payloads.append(obj_in)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(message_platforms_api, "_normalize_create_payload", normalize_create)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "get_by_name", get_by_name)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "create", create)
    monkeypatch.setattr(message_platforms_api, "get_validated_profile_for_assignment", validate_profile)
    _patch_response_serialization(monkeypatch)

    response = await message_platforms_api.create_message_platform(
        MessagePlatformCreate(name="platform-1", is_enabled=True, profile_id=17),
        db=db,
        admin=SimpleNamespace(uid=" owner-1 "),
    )

    assert response.code == 200
    assert calls == [
        ("get_by_name", "platform-1"),
        ("normalize", "platform-1"),
        ("validate", 17, "owner-1"),
    ]
    assert created_payloads == [
        {
            "name": "platform-1",
            "platform_type": MessagePlatformType.WEIXIN_OPENCLAW,
            "is_enabled": True,
            "uid": "owner-1",
            "profile_id": 17,
            "config": {},
            "state": {},
        }
    ]


@pytest.mark.asyncio
async def test_update_message_platform_uid_revalidates_existing_profile(monkeypatch):
    db = FakeDb()
    calls = []
    update_payloads = []
    platform = SimpleNamespace(
        id=3,
        name="platform-1",
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        is_enabled=True,
        uid="user-1",
        profile_id=31,
        config={},
    )

    def normalize_update(platform_type, payload):
        calls.append(("normalize", platform_type, payload.uid))
        return {"uid": payload.uid}

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("get", platform_id))
        return platform

    async def validate_profile(db_arg, *, profile_id, uid):
        assert db_arg is db
        calls.append(("validate", profile_id, uid))
        return SimpleNamespace(id=profile_id, uid=uid)

    async def update(db_arg, *, db_obj, obj_in):
        assert db_arg is db
        assert db_obj is platform
        update_payloads.append(obj_in)
        return platform

    monkeypatch.setattr(message_platforms_api, "_normalize_update_payload", normalize_update)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "update", update)
    monkeypatch.setattr(message_platforms_api, "get_validated_profile_for_assignment", validate_profile)
    _patch_response_serialization(monkeypatch)

    response = await message_platforms_api.update_message_platform(
        3,
        MessagePlatformUpdate(uid=" user-2 "),
        db=db,
    )

    assert response.code == 200
    assert calls == [
        ("get", 3),
        ("normalize", MessagePlatformType.WEIXIN_OPENCLAW, " user-2 "),
        ("validate", 31, "user-2"),
    ]
    assert update_payloads == [{"uid": "user-2"}]


@pytest.mark.asyncio
async def test_update_message_platform_can_clear_profile_without_validation(monkeypatch):
    db = FakeDb()
    update_payloads = []
    platform = SimpleNamespace(
        id=3,
        name="platform-1",
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        is_enabled=True,
        uid="user-1",
        profile_id=31,
        config={},
    )

    def normalize_update(platform_type, payload):
        assert platform_type == MessagePlatformType.WEIXIN_OPENCLAW
        assert payload.profile_id is None
        return {"profile_id": None}

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        assert platform_id == 3
        return platform

    async def validate_profile(*args, **kwargs):
        raise AssertionError("clearing a profile assignment must not validate a profile")

    async def update(db_arg, *, db_obj, obj_in):
        assert db_arg is db
        assert db_obj is platform
        update_payloads.append(obj_in)
        return platform

    monkeypatch.setattr(message_platforms_api, "_normalize_update_payload", normalize_update)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(message_platforms_api.message_platform_crud, "update", update)
    monkeypatch.setattr(message_platforms_api, "get_validated_profile_for_assignment", validate_profile)
    _patch_response_serialization(monkeypatch)

    response = await message_platforms_api.update_message_platform(
        3,
        MessagePlatformUpdate(profile_id=None),
        db=db,
    )

    assert response.code == 200
    assert update_payloads == [{"profile_id": None}]
