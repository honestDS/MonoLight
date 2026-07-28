from types import SimpleNamespace

import pytest

from app.core import profile_selection


@pytest.mark.asyncio
async def test_session_override_profile_takes_priority_over_platform_and_default(monkeypatch):
    db = SimpleNamespace()
    calls = []
    override_profile = SimpleNamespace(id=11, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return SimpleNamespace(uid="user-1", profile_override_id=11)

    async def get_profile(db_arg, profile_id):
        assert db_arg is db
        calls.append(("profile", profile_id))
        return override_profile

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return SimpleNamespace(uid="user-1", profile_id=22)

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return SimpleNamespace(id=1, uid=uid)

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=7,
    )

    assert profile is override_profile
    assert calls == [("session", "session-1"), ("profile", 11)]


@pytest.mark.asyncio
async def test_platform_profile_is_used_when_session_has_no_override(monkeypatch):
    db = SimpleNamespace()
    calls = []
    platform_profile = SimpleNamespace(id=22, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return SimpleNamespace(uid="user-1", profile_override_id=None)

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return SimpleNamespace(uid="user-1", profile_id=22)

    async def get_profile(db_arg, profile_id):
        assert db_arg is db
        calls.append(("profile", profile_id))
        return platform_profile

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return SimpleNamespace(id=1, uid=uid)

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=7,
    )

    assert profile is platform_profile
    assert calls == [("session", "session-1"), ("platform", 7), ("profile", 22)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override_profile",
    [
        SimpleNamespace(id=11, uid="user-2"),
        None,
    ],
    ids=["owned-by-another-user", "missing"],
)
async def test_invalid_session_override_falls_back_to_owned_platform_profile(monkeypatch, override_profile):
    db = SimpleNamespace()
    calls = []
    platform_profile = SimpleNamespace(id=22, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return SimpleNamespace(uid="user-1", profile_override_id=11)

    async def get_profile(db_arg, profile_id):
        assert db_arg is db
        calls.append(("profile", profile_id))
        return {11: override_profile, 22: platform_profile}[profile_id]

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return SimpleNamespace(uid="user-1", profile_id=22)

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return SimpleNamespace(id=1, uid=uid)

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=7,
    )

    assert profile is platform_profile
    assert calls == [
        ("session", "session-1"),
        ("profile", 11),
        ("platform", 7),
        ("profile", 22),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "expected_profile_calls"),
    [
        (SimpleNamespace(uid="user-2", profile_id=22), []),
        (SimpleNamespace(uid="user-1", profile_id=22), [22]),
    ],
    ids=["platform-owned-by-another-user", "platform-profile-missing"],
)
async def test_invalid_platform_assignment_falls_back_to_default(monkeypatch, platform, expected_profile_calls):
    db = SimpleNamespace()
    calls = []
    default_profile = SimpleNamespace(id=1, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return SimpleNamespace(uid="user-1", profile_override_id=None)

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return platform

    async def get_profile(db_arg, profile_id):
        assert db_arg is db
        calls.append(("profile", profile_id))
        return None

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return default_profile

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=7,
    )

    assert profile is default_profile
    assert [profile_id for name, profile_id in calls if name == "profile"] == expected_profile_calls
    assert [call for call in calls if call[0] == "platform"] == [("platform", 7)]
    assert [call for call in calls if call[0] == "default"] == [("default", "user-1")]


@pytest.mark.asyncio
async def test_missing_platform_id_falls_back_directly_to_default(monkeypatch):
    db = SimpleNamespace()
    calls = []
    default_profile = SimpleNamespace(id=1, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return None

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return None

    async def get_profile(db_arg, profile_id):
        assert db_arg is db
        calls.append(("profile", profile_id))
        return None

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return default_profile

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=None,
    )

    assert profile is default_profile
    assert calls == [("session", "session-1"), ("default", "user-1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_profile_id", [0, -1, False, True], ids=["zero", "negative", "false", "true"])
async def test_invalid_profile_ids_are_not_queried(monkeypatch, invalid_profile_id):
    db = SimpleNamespace()
    calls = []
    default_profile = SimpleNamespace(id=1, uid="user-1")

    async def get_session(db_arg, session_id):
        assert db_arg is db
        calls.append(("session", session_id))
        return SimpleNamespace(uid="user-1", profile_override_id=invalid_profile_id)

    async def get_platform(db_arg, platform_id):
        assert db_arg is db
        calls.append(("platform", platform_id))
        return SimpleNamespace(uid="user-1", profile_id=invalid_profile_id)

    async def get_profile(db_arg, profile_id):
        raise AssertionError(f"invalid profile id must not be queried: {profile_id}")

    async def get_default(db_arg, *, uid):
        assert db_arg is db
        calls.append(("default", uid))
        return default_profile

    monkeypatch.setattr(profile_selection.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(profile_selection.message_platform_crud, "get", get_platform)
    monkeypatch.setattr(profile_selection.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(profile_selection.profile_crud, "get_default", get_default)

    profile = await profile_selection.resolve_profile_for_session(
        db,
        uid="user-1",
        session_id="session-1",
        message_platform_id=7,
    )

    assert profile is default_profile
    assert calls == [("session", "session-1"), ("platform", 7), ("default", "user-1")]
