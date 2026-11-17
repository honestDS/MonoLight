import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router
from app.api.v1.users import router as user_router
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.profile.prompt import prompt_crud
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.handler import register_handlers
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db
from app.providers.database.bootstrap import ensure_default_profile_for_user
from tests.integration.profiles import test_prompt_management_permissions as prompt_management

PromptIds = prompt_management.PromptIds
api_context = prompt_management.api_context
prompt_profile_lock_session_factory = prompt_management.prompt_profile_lock_session_factory

_token = prompt_management._token
_prompt_snapshot = prompt_management._prompt_snapshot
_assert_standard = prompt_management._assert_standard
_assert_page = prompt_management._assert_page
_update_prompt = prompt_management._update_prompt
_assert_updated_prompt = prompt_management._assert_updated_prompt
_assert_only_prompt_changed = prompt_management._assert_only_prompt_changed
_profile_snapshot = prompt_management._profile_snapshot
_seed_profiles = prompt_management._seed_profiles
_create_profile = prompt_management._create_profile
_delete_prompt = prompt_management._delete_prompt
_create_prompt = prompt_management._create_prompt

ERR_PROMPT_NAME_EXISTS = prompt_management.ERR_PROMPT_NAME_EXISTS
ERR_PROMPT_OWNER_REQUIRED = prompt_management.ERR_PROMPT_OWNER_REQUIRED
t = prompt_management.t


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt_owner",
    (
        pytest.param("user-a", id="user-a"),
        pytest.param(None, id="orphan"),
        pytest.param("deleted-user", id="legacy"),
    ),
)
async def test_prompt_update_superuser_transfers_unreferenced_prompt_to_user_b_without_changing_content(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    prompt_owner: str | None,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids[prompt_owner][0]
    before = await _prompt_snapshot(session_factory)
    before_row = {row[0]: row for row in before}[prompt_id]
    original_name = before_row[2]
    original_content = before_row[3]
    original_created_at = before_row[4]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        transfer_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {"uid": "user-b"},
        )
        transfer_data = _assert_updated_prompt(
            transfer_response,
            uid="user-b",
            name=original_name,
            content=original_content,
        )
        assert transfer_data["id"] == prompt_id
        assert transfer_data["created_at"] == original_created_at.isoformat()
        assert transfer_data["username"] == "bob"

        after_transfer = await _prompt_snapshot(session_factory)
        _assert_only_prompt_changed(
            before,
            after_transfer,
            prompt_id,
            uid="user-b",
            name=original_name,
            content=original_content,
        )

        bob_list_response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('bob')}"},
            params={"page": 1, "size": 100},
        )
        bob_list = _assert_page(bob_list_response, page=1, size=100, current_uid="user-b")
        bob_items = [item for item in bob_list["items"] if item["id"] == prompt_id]
        assert len(bob_items) == 1
        assert bob_items[0]["uid"] == "user-b"
        assert bob_items[0]["username"] == "bob"

        alice_list_response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('alice')}"},
            params={"page": 1, "size": 100},
        )
        alice_list = _assert_page(alice_list_response, page=1, size=100, current_uid="user-a")
        assert prompt_id not in {item["id"] for item in alice_list["items"]}

        before_alice_edit = await _prompt_snapshot(session_factory)
        alice_edit_response = await _update_prompt(
            client,
            _token("alice"),
            prompt_id,
            {"name": "alice-forbidden-edit", "content": "not persisted"},
        )
        _assert_standard(alice_edit_response, 403, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_alice_edit

        bob_name = f"{original_name}-bob-edit"
        bob_content = f"{original_content}-bob-edit"
        before_bob_edit = await _prompt_snapshot(session_factory)
        bob_edit_response = await _update_prompt(
            client,
            _token("bob"),
            prompt_id,
            {"name": bob_name, "content": bob_content},
        )
        bob_edit_data = _assert_updated_prompt(
            bob_edit_response,
            uid="user-b",
            name=bob_name,
            content=bob_content,
        )
        assert bob_edit_data["id"] == prompt_id
        assert bob_edit_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_bob_edit,
        await _prompt_snapshot(session_factory),
        prompt_id,
        uid="user-b",
        name=bob_name,
        content=bob_content,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_uid", ("user-b", "admin", "deleted-user"))
async def test_prompt_update_regular_user_cannot_change_own_prompt_owner_in_json(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    requested_uid: str,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    forged_token = _token("alice", is_superuser=True, uid="user-b")

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            forged_token,
            prompt_id,
            {
                "uid": requested_uid,
                "name": f"forbidden-{requested_uid}",
                "content": "not persisted",
            },
        )

    _assert_standard(response, 403, data_is_none=True)
    assert await _prompt_snapshot(session_factory) == before_prompts
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_uid, expected_status",
    (
        pytest.param(None, 400, id="null"),
        pytest.param("deleted-user", 404, id="missing-user"),
        pytest.param("", 422, id="empty"),
        pytest.param("x" * 51, 422, id="too-long"),
    ),
)
async def test_prompt_update_superuser_rejects_invalid_owner_uid_without_prompt_or_profile_changes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    requested_uid: str | None,
    expected_status: int,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {
                "uid": requested_uid,
                "name": "invalid-owner-update",
                "content": "not persisted",
            },
        )

    payload = _assert_standard(response, expected_status, data_is_none=True)
    if requested_uid is None:
        assert payload["message"] == t(ERR_PROMPT_OWNER_REQUIRED)
    assert await _prompt_snapshot(session_factory) == before_prompts
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("include_name", (False, True), ids=("actual-name", "explicit-name"))
async def test_prompt_update_superuser_checks_transferred_name_against_target_owner_scope(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    include_name: bool,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    async with session_factory() as session:
        prompt = (await session.execute(select(PromptLibrary).where(PromptLibrary.id == prompt_id))).scalars().one()
        original_content = prompt.content
        prompt.name = "bob-one"
        await session.commit()
    renamed_row = {row[0]: row for row in await _prompt_snapshot(session_factory)}[prompt_id]
    assert renamed_row[3] == original_content

    conflict_body: dict[str, Any] = {"uid": "user-b", "content": "not persisted"}
    if include_name:
        conflict_body["name"] = "bob-two"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_conflict_prompts = await _prompt_snapshot(session_factory)
        before_conflict_profiles = await _profile_snapshot(session_factory)
        conflict_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            conflict_body,
        )
        _assert_standard(conflict_response, 400, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_conflict_prompts
        assert await _profile_snapshot(session_factory) == before_conflict_profiles

        transfer_name = "bob-transferred-unique"
        transfer_content = "bob-transferred-content"
        before_transfer_prompts = await _prompt_snapshot(session_factory)
        before_transfer_profiles = await _profile_snapshot(session_factory)
        transfer_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {"uid": "user-b", "name": transfer_name, "content": transfer_content},
        )
        transfer_data = _assert_updated_prompt(
            transfer_response,
            uid="user-b",
            name=transfer_name,
            content=transfer_content,
        )
        assert transfer_data["id"] == prompt_id
        assert transfer_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_transfer_prompts,
        await _prompt_snapshot(session_factory),
        prompt_id,
        uid="user-b",
        name=transfer_name,
        content=transfer_content,
    )
    assert await _profile_snapshot(session_factory) == before_transfer_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ("zh", "en"))
async def test_prompt_update_protects_referenced_prompt_until_profile_repoints(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    locale: str,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    source_id, replacement_id = prompt_ids["user-a"]
    (profile_id,) = await _seed_profiles(session_factory, (("user-a", source_id, "alice-reference"),))

    same_owner_content = "content-alice-one-updated"
    before_same_owner_prompts = await _prompt_snapshot(session_factory)
    before_same_owner_profiles = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        same_owner_response = await _update_prompt(
            client,
            _token("super-admin"),
            source_id,
            {"uid": "user-a", "content": same_owner_content},
        )
        same_owner_data = _assert_updated_prompt(
            same_owner_response,
            uid="user-a",
            name="alice-one",
            content=same_owner_content,
        )
        assert same_owner_data["id"] == source_id
        assert same_owner_data["username"] == "alice"

        after_same_owner_prompts = await _prompt_snapshot(session_factory)
        _assert_only_prompt_changed(
            before_same_owner_prompts,
            after_same_owner_prompts,
            source_id,
            uid="user-a",
            name="alice-one",
            content=same_owner_content,
        )
        assert await _profile_snapshot(session_factory) == before_same_owner_profiles

        transfer_name = "bob-transferred-reference"
        transfer_content = "content-bob-transferred-reference"
        before_conflict_prompts = await _prompt_snapshot(session_factory)
        before_conflict_profiles = await _profile_snapshot(session_factory)
        locale_token = set_current_locale(locale)
        try:
            conflict_response = await _update_prompt(
                client,
                _token("super-admin"),
                source_id,
                {"uid": "user-b", "name": transfer_name, "content": transfer_content},
            )
        finally:
            reset_current_locale(locale_token)

        conflict_payload = _assert_standard(conflict_response, 409, data_is_none=True)
        assert (
            conflict_payload["message"]
            == {
                "zh": "提示词正在被配置引用，请先为相关配置更换同一所属用户的提示词，再修改所属用户",
                "en": "This prompt is referenced by profiles. Select another prompt belonging to the same owner for those profiles before changing its owner.",
            }[locale]
        )
        assert await _prompt_snapshot(session_factory) == before_conflict_prompts
        assert await _profile_snapshot(session_factory) == before_conflict_profiles

        profile_response = await client.post(
            "/api/v1/profiles/update",
            headers={"Authorization": f"Bearer {_token('super-admin')}"},
            params={"profile_id": profile_id},
            json={"prompt_id": replacement_id},
        )
        profile_payload = _assert_standard(profile_response, 200)
        assert profile_payload["data"]["id"] == profile_id
        assert profile_payload["data"]["uid"] == "user-a"
        assert profile_payload["data"]["prompt_id"] == replacement_id

        before_retry_prompts = await _prompt_snapshot(session_factory)
        before_retry_profiles = await _profile_snapshot(session_factory)
        retry_response = await _update_prompt(
            client,
            _token("super-admin"),
            source_id,
            {"uid": "user-b", "name": transfer_name, "content": transfer_content},
        )
        retry_data = _assert_updated_prompt(
            retry_response,
            uid="user-b",
            name=transfer_name,
            content=transfer_content,
        )
        assert retry_data["id"] == source_id
        assert retry_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_retry_prompts,
        await _prompt_snapshot(session_factory),
        source_id,
        uid="user-b",
        name=transfer_name,
        content=transfer_content,
    )
    after_retry_profiles = await _profile_snapshot(session_factory)
    assert after_retry_profiles == before_retry_profiles
    profile_row = next(row for row in after_retry_profiles if row[0] == profile_id)
    assert profile_row[1] == "user-a"
    assert profile_row[3] == replacement_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_operation, expected_status",
    (
        pytest.param("profile-create", 400, id="profile-create"),
        pytest.param("profile-update", 404, id="profile-update"),
        pytest.param("prompt-update", 403, id="prompt-update"),
        pytest.param("prompt-delete", 403, id="prompt-delete"),
    ),
)
async def test_prompt_transfer_serializes_old_owner_operations_and_rejects_concurrent_access(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    old_operation: str,
    expected_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    source_id = prompt_ids["user-a"][0]
    profile_id: int | None = None
    if old_operation == "profile-update":
        (profile_id,) = await _seed_profiles(
            session_factory,
            (("user-a", prompt_ids["user-a"][1], "alice-concurrent-profile"),),
        )

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    source_row = next(row for row in before_prompts if row[0] == source_id)
    source_name = source_row[2]
    source_content = source_row[3]
    source_created_at = source_row[4]

    transfer_staged = asyncio.Event()
    release_transfer = asyncio.Event()
    waiter_started = asyncio.Event()
    original_prompt_update = prompt_crud.update
    original_prompt_get = prompt_crud.get
    original_profile_lock = profile_crud.lock_for_runtime_use
    original_user_get = user_crud.get_by_uid

    async def gated_prompt_update(
        db: AsyncSession,
        *,
        db_obj: PromptLibrary,
        obj_in: Any,
        commit: bool = True,
    ) -> PromptLibrary:
        result = await original_prompt_update(db, db_obj=db_obj, obj_in=obj_in, commit=commit)
        if commit is False and getattr(obj_in, "uid", None) == "user-b":
            transfer_staged.set()
            await release_transfer.wait()
        return result

    async def gated_prompt_get(
        db: AsyncSession,
        id: int,
        *,
        lock_for_reference_write: bool = False,
    ) -> PromptLibrary | None:
        if transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_prompt_get(db, id, lock_for_reference_write=lock_for_reference_write)

    async def gated_profile_lock(
        db: AsyncSession,
        *,
        profile_id: int,
        uid: str | None,
    ) -> Profile | None:
        if transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_profile_lock(db, profile_id=profile_id, uid=uid)

    async def gated_user_get(
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        if lock_for_reference_write and transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_user_get(db, uid, lock_for_reference_write=lock_for_reference_write)

    monkeypatch.setattr(prompt_crud, "update", gated_prompt_update)
    monkeypatch.setattr(prompt_crud, "get", gated_prompt_get)
    monkeypatch.setattr(profile_crud, "lock_for_runtime_use", gated_profile_lock)
    monkeypatch.setattr(user_crud, "get_by_uid", gated_user_get)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def run_old_operation() -> httpx.Response:
            if old_operation == "profile-create":
                return await _create_profile(
                    client,
                    _token("alice"),
                    {"name": "alice-concurrent-profile-create", "prompt_id": source_id},
                )
            if old_operation == "profile-update":
                assert profile_id is not None
                return await client.post(
                    "/api/v1/profiles/update",
                    headers={"Authorization": f"Bearer {_token('alice')}"},
                    params={"profile_id": profile_id},
                    json={"prompt_id": source_id},
                )
            if old_operation == "prompt-update":
                return await _update_prompt(
                    client,
                    _token("alice"),
                    source_id,
                    {"name": "alice-concurrent-update", "content": "alice-concurrent-content"},
                )
            if old_operation == "prompt-delete":
                return await _delete_prompt(client, _token("alice"), source_id)
            raise AssertionError(f"unsupported operation: {old_operation}")

        admin_task: asyncio.Task[httpx.Response] | None = None
        old_task: asyncio.Task[httpx.Response] | None = None
        try:
            admin_task = asyncio.create_task(_update_prompt(client, _token("super-admin"), source_id, {"uid": "user-b"}))
            await asyncio.wait_for(transfer_staged.wait(), 10)

            old_task = asyncio.create_task(run_old_operation())
            await asyncio.wait_for(waiter_started.wait(), 10)
            assert not old_task.done()

            release_transfer.set()
            admin_response, old_response = await asyncio.wait_for(
                asyncio.gather(admin_task, old_task),
                10,
            )
        finally:
            release_transfer.set()
            for task in (admin_task, old_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (admin_task, old_task) if task is not None),
                return_exceptions=True,
            )

    admin_data = _assert_updated_prompt(
        admin_response,
        uid="user-b",
        name=source_name,
        content=source_content,
    )
    assert admin_data["username"] == "bob"
    assert admin_data["created_at"] == source_created_at.isoformat()
    _assert_standard(old_response, expected_status, data_is_none=True)

    after_prompts = await _prompt_snapshot(session_factory)
    _assert_only_prompt_changed(
        before_prompts,
        after_prompts,
        source_id,
        uid="user-b",
        name=source_name,
        content=source_content,
    )
    after_source = next(row for row in after_prompts if row[0] == source_id)
    assert after_source[4] == source_created_at
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    (
        pytest.param("profile-create-no-prompt", id="profile-create-no-prompt"),
        pytest.param("profile-create-with-prompt", id="profile-create-with-prompt"),
        pytest.param("prompt-create", id="prompt-create"),
        pytest.param("prompt-transfer", id="prompt-transfer"),
        pytest.param("default-bootstrap", id="default-bootstrap"),
    ),
)
async def test_user_delete_blocks_concurrent_configuration_creation_and_prompt_writes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(user_router, prefix="/api/v1/admin")
    app.include_router(profile_router, prefix="/api/v1")

    user_a_prompt_id = prompt_ids["user-a"][0]
    source_transfer = prompt_ids["user-b"][0]
    (default_profile_id,) = await _seed_profiles(
        session_factory,
        (("user-a", user_a_prompt_id, "defaultProfile"),),
    )
    async with session_factory() as session:
        default_profile = (await session.execute(select(Profile).where(Profile.id == default_profile_id))).scalars().one()
        default_profile.is_default = True
        user_a = (await session.execute(select(User).where(User.uid == "user-a"))).scalars().one()
        assert user_a.id is not None
        user_a_id = int(user_a.id)
        await session.commit()

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    source_before = next(row for row in before_prompts if row[0] == source_transfer)

    deletion_staged = asyncio.Event()
    release = asyncio.Event()
    waiter_started = asyncio.Event()
    original_remove = user_crud.remove
    original_get_by_uid = user_crud.get_by_uid

    async def gated_remove(db: AsyncSession, *, id: Any) -> User | None:
        if id == user_a_id:
            deletion_staged.set()
            await release.wait()
        return await original_remove(db, id=id)

    async def gated_get_by_uid(
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        if lock_for_reference_write and deletion_staged.is_set() and not release.is_set():
            waiter_started.set()
        return await original_get_by_uid(db, uid, lock_for_reference_write=lock_for_reference_write)

    monkeypatch.setattr(user_crud, "remove", gated_remove)
    monkeypatch.setattr(user_crud, "get_by_uid", gated_get_by_uid)

    admin_token = _token("super-admin")
    delete_task: asyncio.Task[httpx.Response] | None = None
    operation_task: asyncio.Task[Any] | None = None
    delete_response: httpx.Response | None = None
    operation_result: Any = None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def run_operation() -> httpx.Response | None:
            if operation == "profile-create-no-prompt":
                return await _create_profile(
                    client,
                    admin_token,
                    {"uid": "user-a", "name": "concurrent-profile-no-prompt"},
                )
            if operation == "profile-create-with-prompt":
                return await _create_profile(
                    client,
                    admin_token,
                    {
                        "uid": "user-a",
                        "name": "concurrent-profile-with-prompt",
                        "prompt_id": user_a_prompt_id,
                    },
                )
            if operation == "prompt-create":
                return await _create_prompt(
                    client,
                    admin_token,
                    {"uid": "user-a", "name": "concurrent-prompt", "content": "not persisted"},
                )
            if operation == "prompt-transfer":
                return await _update_prompt(client, admin_token, source_transfer, {"uid": "user-a"})
            if operation == "default-bootstrap":
                async with session_factory() as session:
                    result = await ensure_default_profile_for_user(session, uid="user-a")
                    await session.commit()
                    return result
            raise AssertionError(f"unsupported operation: {operation}")

        try:
            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    params={"uid": "user-a"},
                )
            )
            await asyncio.wait_for(deletion_staged.wait(), 10)

            operation_task = asyncio.create_task(run_operation())
            await asyncio.wait_for(waiter_started.wait(), 10)
            assert not operation_task.done()

            release.set()
            assert delete_task is not None
            assert operation_task is not None
            delete_response, operation_result = await asyncio.wait_for(
                asyncio.gather(delete_task, operation_task),
                10,
            )
        finally:
            release.set()
            for task in (delete_task, operation_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (delete_task, operation_task) if task is not None),
                return_exceptions=True,
            )

    assert delete_response is not None
    _assert_standard(delete_response, 200, data_is_none=True)
    if operation == "default-bootstrap":
        assert operation_result is None
    else:
        assert isinstance(operation_result, httpx.Response)
        _assert_standard(operation_result, 404, data_is_none=True)

    after_prompts = await _prompt_snapshot(session_factory)
    after_profiles = await _profile_snapshot(session_factory)
    assert all(row[1] != "user-a" for row in after_prompts)
    assert all(row[1] != "user-a" for row in after_profiles)
    assert tuple(row for row in after_prompts if row[1] != "user-a") == tuple(row for row in before_prompts if row[1] != "user-a")
    assert tuple(row for row in after_profiles if row[1] != "user-a") == tuple(row for row in before_profiles if row[1] != "user-a")

    source_after = next(row for row in after_prompts if row[0] == source_transfer)
    assert source_after[1] == "user-b"
    assert source_after[2:4] == source_before[2:4]
    async with session_factory() as session:
        assert (await session.execute(select(User).where(User.uid == "user-a"))).scalars().first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "create_a_first",
    (
        pytest.param(True, id="a-lower-id"),
        pytest.param(False, id="b-lower-id"),
    ),
)
async def test_profile_update_to_replacement_serializes_with_prompt_delete(
    prompt_profile_lock_session_factory: async_sessionmaker[AsyncSession],
    create_a_first: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = prompt_profile_lock_session_factory
    test_uid = f"lock-test-{uuid.uuid4().hex[:12]}"
    profile_a_id: int | None = None
    profile_b_id: int | None = None
    prompt_a_id: int | None = None
    prompt_b_id: int | None = None
    prompt_b_name = f"{test_uid}-prompt-b"
    prompt_b_content = f"{test_uid}-content-b"
    profile_b_name = f"{test_uid}-profile-b"
    updated_profile_name = f"{test_uid}-profile-a-updated"

    try:
        async with session_factory() as session:
            test_user = User(
                uid=test_uid,
                username=f"{test_uid}-user",
                is_active=True,
                is_superuser=False,
            )
            prompt_a = PromptLibrary(
                uid=test_uid,
                name=f"{test_uid}-prompt-a",
                content=f"{test_uid}-content-a",
            )
            prompt_b = PromptLibrary(
                uid=test_uid,
                name=prompt_b_name,
                content=prompt_b_content,
            )
            session.add(test_user)
            if create_a_first:
                session.add(prompt_a)
                await session.flush()
                session.add(prompt_b)
            else:
                session.add(prompt_b)
                await session.flush()
                session.add(prompt_a)
            await session.flush()
            assert prompt_a.id is not None
            assert prompt_b.id is not None
            prompt_a_id = int(prompt_a.id)
            prompt_b_id = int(prompt_b.id)

            profile_a = Profile(
                uid=test_uid,
                name=f"{test_uid}-profile-a",
                prompt_id=prompt_a_id,
                configs={},
                is_default=False,
            )
            profile_b = Profile(
                uid=test_uid,
                name=profile_b_name,
                prompt_id=prompt_b_id,
                configs={},
                is_default=False,
            )
            session.add_all([profile_a, profile_b])
            await session.flush()
            assert profile_a.id is not None
            assert profile_b.id is not None
            profile_a_id = int(profile_a.id)
            profile_b_id = int(profile_b.id)
            await session.commit()

        app = FastAPI()
        register_handlers(app)
        app.include_router(profile_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    if session.in_transaction():
                        await session.rollback()

        async def override_current_user() -> User:
            return test_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[security.get_current_user] = override_current_user

        assert prompt_a_id is not None
        assert prompt_b_id is not None
        assert profile_a_id is not None
        assert profile_b_id is not None
        update_at_prompt = asyncio.Event()
        delete_at_profiles = asyncio.Event()
        release = asyncio.Event()
        original_get_visible = prompt_crud.get_visible
        original_get_multi_by_prompt_id = profile_crud.get_multi_by_prompt_id

        async def gated_get_visible(
            db: AsyncSession,
            prompt_id: int,
            uid: str | None = None,
            *,
            lock_for_reference_write: bool = False,
        ) -> PromptLibrary | None:
            if prompt_id == prompt_b_id and uid == test_uid and lock_for_reference_write:
                update_at_prompt.set()
                await asyncio.wait_for(release.wait(), 10)
            return await original_get_visible(
                db,
                prompt_id,
                uid=uid,
                lock_for_reference_write=lock_for_reference_write,
            )

        async def gated_get_multi_by_prompt_id(
            db: AsyncSession,
            prompt_id: int,
            *,
            lock_for_reference_write: bool = False,
        ) -> list[Profile]:
            if prompt_id == prompt_a_id and lock_for_reference_write:
                delete_at_profiles.set()
                await asyncio.wait_for(release.wait(), 10)
            return await original_get_multi_by_prompt_id(
                db,
                prompt_id,
                lock_for_reference_write=lock_for_reference_write,
            )

        monkeypatch.setattr(prompt_crud, "get_visible", gated_get_visible)
        monkeypatch.setattr(profile_crud, "get_multi_by_prompt_id", gated_get_multi_by_prompt_id)

        update_response: httpx.Response | None = None
        delete_response: httpx.Response | None = None
        update_task: asyncio.Task[httpx.Response] | None = None
        delete_task: asyncio.Task[httpx.Response] | None = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            try:
                update_task = asyncio.create_task(
                    client.post(
                        "/api/v1/profiles/update",
                        params={"profile_id": profile_a_id},
                        json={"name": updated_profile_name, "prompt_id": prompt_b_id},
                    )
                )
                await asyncio.wait_for(update_at_prompt.wait(), 10)

                delete_task = asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/delete",
                        params={
                            "prompt_id": prompt_a_id,
                            "replacement_prompt_id": prompt_b_id,
                            "confirm_reassign": True,
                        },
                    )
                )
                await asyncio.wait_for(delete_at_profiles.wait(), 10)
                assert not update_task.done()
                assert not delete_task.done()

                release.set()
                update_response, delete_response = await asyncio.wait_for(
                    asyncio.gather(update_task, delete_task),
                    10,
                )
            finally:
                release.set()
                tasks = [task for task in (update_task, delete_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        10,
                    )

        assert update_response is not None
        assert delete_response is not None
        update_payload = _assert_standard(update_response, 200)
        assert update_payload["data"]["prompt_id"] == prompt_b_id
        assert update_payload["data"]["name"] == updated_profile_name
        _assert_standard(delete_response, 200, data_is_none=True)

        async with session_factory() as session:
            prompts = (await session.execute(select(PromptLibrary).where(PromptLibrary.uid == test_uid))).scalars().all()
            profiles = (await session.execute(select(Profile).where(Profile.uid == test_uid).order_by(Profile.id))).scalars().all()

        assert {prompt.id for prompt in prompts} == {prompt_b_id}
        replacement_prompt = prompts[0]
        assert replacement_prompt.uid == test_uid
        assert replacement_prompt.name == prompt_b_name
        assert replacement_prompt.content == prompt_b_content

        profiles_by_id = {profile.id: profile for profile in profiles}
        assert profile_a_id in profiles_by_id
        assert profile_b_id in profiles_by_id
        updated_profile = profiles_by_id[profile_a_id]
        assert updated_profile.uid == test_uid
        assert updated_profile.prompt_id == prompt_b_id
        assert updated_profile.name == updated_profile_name
        untouched_profile = profiles_by_id[profile_b_id]
        assert untouched_profile.uid == test_uid
        assert untouched_profile.prompt_id == prompt_b_id
        assert untouched_profile.name == profile_b_name
        assert untouched_profile.configs == {}
        assert untouched_profile.is_default is False
    finally:
        async with session_factory() as session:
            await session.execute(delete(Profile).where(Profile.uid == test_uid))
            await session.execute(delete(PromptLibrary).where(PromptLibrary.uid == test_uid))
            await session.execute(delete(User).where(User.uid == test_uid))
            await session.commit()


@pytest.mark.asyncio
async def test_prompt_concurrent_rename_keeps_owner_names_unique(
    prompt_profile_lock_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = prompt_profile_lock_session_factory
    test_uid = f"rename-lock-{uuid.uuid4().hex[:12]}"
    prompt_a_id: int | None = None
    prompt_b_id: int | None = None
    request_tasks: list[asyncio.Task[httpx.Response]] = []
    responses: list[httpx.Response] | None = None
    name_queries_returned = asyncio.Event()
    release_name_queries = asyncio.Event()
    renamed_name = f"{test_uid}-renamed"
    updated_contents: dict[int, str] = {}
    original_rows: dict[int, tuple[str | None, str, str, datetime]] = {}

    try:
        async with session_factory() as session:
            is_mysql = session.get_bind().dialect.name == "mysql"
            test_user = User(
                uid=test_uid,
                username=f"{test_uid}-user",
                is_active=True,
                is_superuser=False,
            )
            prompt_a = PromptLibrary(
                uid=test_uid,
                name=f"{test_uid}-prompt-a",
                content=f"{test_uid}-content-a",
            )
            prompt_b = PromptLibrary(
                uid=test_uid,
                name=f"{test_uid}-prompt-b",
                content=f"{test_uid}-content-b",
            )
            session.add_all([test_user, prompt_a, prompt_b])
            await session.flush()
            assert prompt_a.id is not None
            assert prompt_b.id is not None
            prompt_a_id = int(prompt_a.id)
            prompt_b_id = int(prompt_b.id)
            updated_contents = {
                prompt_a_id: f"{test_uid}-updated-content-a",
                prompt_b_id: f"{test_uid}-updated-content-b",
            }
            await session.commit()
            await session.refresh(prompt_a)
            await session.refresh(prompt_b)
            original_rows = {
                prompt_a_id: (prompt_a.uid, prompt_a.name, prompt_a.content, prompt_a.created_at),
                prompt_b_id: (prompt_b.uid, prompt_b.name, prompt_b.content, prompt_b.created_at),
            }

        app = FastAPI()
        register_handlers(app)
        app.include_router(router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    if session.in_transaction():
                        await session.rollback()

        async def override_current_user() -> User:
            return test_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[security.get_current_user] = override_current_user

        original_get_by_name = prompt_crud.get_by_name
        name_query_count = 0

        async def gated_get_by_name(
            db: AsyncSession,
            name: str,
            uid: str | None = None,
        ) -> PromptLibrary | None:
            nonlocal name_query_count
            result = await original_get_by_name(db, name, uid=uid)
            if is_mysql and uid == test_uid and name == renamed_name:
                name_query_count += 1
                if name_query_count >= 2:
                    name_queries_returned.set()
                await asyncio.wait_for(release_name_queries.wait(), 10)
            return result

        monkeypatch.setattr(prompt_crud, "get_by_name", gated_get_by_name)

        assert prompt_a_id is not None
        assert prompt_b_id is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request_tasks = [
                asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/update",
                        params={"prompt_id": prompt_a_id},
                        json={"name": renamed_name, "content": updated_contents[prompt_a_id]},
                    )
                ),
                asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/update",
                        params={"prompt_id": prompt_b_id},
                        json={"name": renamed_name, "content": updated_contents[prompt_b_id]},
                    )
                ),
            ]
            if is_mysql:
                await asyncio.wait_for(name_queries_returned.wait(), 10)
                release_name_queries.set()
            responses = list(await asyncio.wait_for(asyncio.gather(*request_tasks), 10))

        assert responses is not None
        assert sorted(response.status_code for response in responses) == [200, 400]
        winner_response = next(response for response in responses if response.status_code == 200)
        loser_response = next(response for response in responses if response.status_code == 400)
        winner_payload = _assert_standard(winner_response, 200)
        loser_payload = _assert_standard(loser_response, 400, data_is_none=True)
        assert loser_payload["message"] == t(ERR_PROMPT_NAME_EXISTS)

        async with session_factory() as session:
            prompts = (await session.execute(select(PromptLibrary).where(PromptLibrary.uid == test_uid).order_by(PromptLibrary.id))).scalars().all()

        assert {prompt.id for prompt in prompts} == {prompt_a_id, prompt_b_id}
        assert sum(prompt.name == renamed_name for prompt in prompts) == 1
        winner = next(prompt for prompt in prompts if prompt.name == renamed_name)
        loser = next(prompt for prompt in prompts if prompt.id != winner.id)
        assert winner.id in original_rows
        assert loser.id in original_rows
        winner_original = original_rows[winner.id]
        loser_original = original_rows[loser.id]
        assert winner.uid == winner_original[0]
        assert winner.name == renamed_name
        assert winner.content == updated_contents[winner.id]
        assert winner.created_at == winner_original[3]
        assert (loser.uid, loser.name, loser.content, loser.created_at) == loser_original

        winner_data = winner_payload["data"]
        assert winner_data["id"] == winner.id
        assert winner_data["uid"] == test_uid
        assert winner_data["name"] == renamed_name
        assert winner_data["content"] == updated_contents[winner.id]
    finally:
        release_name_queries.set()
        for task in request_tasks:
            if not task.done():
                task.cancel()
        if request_tasks:
            await asyncio.wait_for(
                asyncio.gather(*request_tasks, return_exceptions=True),
                10,
            )
        async with session_factory() as session:
            await session.execute(delete(PromptLibrary).where(PromptLibrary.uid == test_uid))
            await session.execute(delete(User).where(User.uid == test_uid))
            await session.commit()


@pytest.mark.asyncio
async def test_prompt_concurrent_ownerless_updates_require_owner(
    prompt_profile_lock_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_factory = prompt_profile_lock_session_factory
    test_uid = f"ownerless-update-{uuid.uuid4().hex[:12]}"
    prompt_ids: tuple[int, ...] = ()
    original_rows: dict[int, tuple[str | None, str, str, datetime]] = {}
    request_tasks: list[asyncio.Task[httpx.Response]] = []

    try:
        async with session_factory() as session:
            test_user = User(
                uid=test_uid,
                username=f"{test_uid}-user",
                is_active=True,
                is_superuser=True,
            )
            prompt_a = PromptLibrary(
                uid=None,
                name=f"{test_uid}-prompt-a",
                content=f"{test_uid}-content-a",
            )
            prompt_b = PromptLibrary(
                uid=None,
                name=f"{test_uid}-prompt-b",
                content=f"{test_uid}-content-b",
            )
            session.add_all([test_user, prompt_a, prompt_b])
            await session.flush()
            assert prompt_a.id is not None
            assert prompt_b.id is not None
            prompt_ids = (int(prompt_a.id), int(prompt_b.id))
            await session.commit()
            await session.refresh(prompt_a)
            await session.refresh(prompt_b)
            original_rows = {
                int(prompt_a.id): (prompt_a.uid, prompt_a.name, prompt_a.content, prompt_a.created_at),
                int(prompt_b.id): (prompt_b.uid, prompt_b.name, prompt_b.content, prompt_b.created_at),
            }

        app = FastAPI()
        register_handlers(app)
        app.include_router(router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    if session.in_transaction():
                        await session.rollback()

        async def override_current_user() -> User:
            return test_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[security.get_current_user] = override_current_user

        shared_name = f"{test_uid}-shared-name"
        updated_contents = (
            f"{test_uid}-updated-content-a",
            f"{test_uid}-updated-content-b",
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request_tasks = [
                asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/update",
                        params={"prompt_id": prompt_ids[0]},
                        json={"name": shared_name, "content": updated_contents[0]},
                    )
                ),
                asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/update",
                        params={"prompt_id": prompt_ids[1]},
                        json={"name": shared_name, "content": updated_contents[1]},
                    )
                ),
            ]
            responses = await asyncio.wait_for(asyncio.gather(*request_tasks), 10)

        assert len(responses) == 2
        for response in responses:
            payload = _assert_standard(response, 400, data_is_none=True)
            assert payload["message"] == t(ERR_PROMPT_OWNER_REQUIRED)

        async with session_factory() as session:
            persisted_prompts = (await session.execute(select(PromptLibrary).where(PromptLibrary.id.in_(prompt_ids)).order_by(PromptLibrary.id))).scalars().all()

        assert len(persisted_prompts) == 2
        after_rows: dict[int, tuple[str | None, str, str, datetime]] = {}
        for prompt in persisted_prompts:
            assert prompt.id is not None
            after_rows[int(prompt.id)] = (prompt.uid, prompt.name, prompt.content, prompt.created_at)
        assert after_rows == original_rows
        assert all(prompt.name != shared_name for prompt in persisted_prompts)
    finally:
        for task in request_tasks:
            if not task.done():
                task.cancel()
        if request_tasks:
            await asyncio.gather(*request_tasks, return_exceptions=True)
        async with session_factory() as session:
            if prompt_ids:
                await session.execute(delete(PromptLibrary).where(PromptLibrary.id.in_(prompt_ids)))
            await session.execute(delete(User).where(User.uid == test_uid))
            await session.commit()
