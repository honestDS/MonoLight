import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1 import auth, knowledge_base
from app.core.constants import ERR_SESSION_NO_PERMISSION, ERR_UNAUTHORIZED, ERR_USER_NOT_FOUND
from app.core.dispatchers.context import build_dispatch_context
from app.core.i18n import t
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.tools.longterm_memory import LongTermMemoryExecutor
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db

TEST_PASSWORD = "integration-password"
TEST_JWT_SECRET = "knowledge-base-permissions-integration-jwt-secret"
KNOWLEDGE_BASE_PREFIX = "/api/v1/knowledge-base"

_TABLES = (
    User.__table__,
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    LongTermMemoryStore.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseProfileBinding.__table__,
    KnowledgeBaseDocument.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
)

_SNAPSHOT_MODELS = (
    User,
    PromptLibrary,
    ModelChannel,
    Profile,
    LongTermMemoryStore,
    KnowledgeBase,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseDocument,
    KnowledgeBaseCollectionOwner,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    KnowledgeJob,
)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return security.get_password_hash(TEST_PASSWORD)


def _knowledge_base(
    *,
    uid: str,
    name: str,
    channel_id: int,
    knowledge_base_type: KnowledgeBaseType,
    managed_profile_id: int | None,
) -> KnowledgeBase:
    collection_name = f"permission-{uid}-{name}-collection"
    active_collection_name = f"permission-{uid}-{name}-active"
    return KnowledgeBase(
        uid=uid,
        name=name,
        description=f"{uid} {name} description",
        embedding_channel_id=channel_id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name=collection_name,
        knowledge_base_type=knowledge_base_type,
        managed_profile_id=managed_profile_id,
        active_embedding_channel_id=channel_id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature=f"permission-signature-{uid}-{name}",
        active_embedding_revision=1,
        active_collection_name=active_collection_name,
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )


async def _seed_database(session: AsyncSession, password_hash: str) -> dict[str, Any]:
    users = [
        User(uid="super-user", username="super-admin", is_active=True, is_superuser=True, hashed_password=password_hash),
        User(uid="normal-user", username="normal-user", is_active=True, is_superuser=False, hashed_password=password_hash),
        User(uid="peer-user", username="peer-user", is_active=True, is_superuser=False, hashed_password=password_hash),
        User(uid="disabled-user", username="disabled-user", is_active=False, is_superuser=False, hashed_password=password_hash),
    ]
    source_channel = ModelChannel(
        name="permission-embedding-source",
        api_key="enc:v1:permission-source-key",
        base_url="https://embedding.invalid/v1",
        model_ids=[
            {
                "model_id": "embed-v1",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 768,
                "is_enabled": True,
            }
        ],
    )
    target_channel = ModelChannel(
        name="permission-embedding-target",
        api_key="enc:v1:permission-target-key",
        base_url="https://embedding-target.invalid/v1",
        model_ids=[
            {
                "model_id": "embed-v2",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
                "is_enabled": True,
            }
        ],
    )
    session.add_all([*users, source_channel, target_channel])
    await session.flush()
    assert source_channel.id is not None
    assert target_channel.id is not None

    resources: dict[str, Any] = {
        "channels": {
            "source_id": source_channel.id,
            "target_id": target_channel.id,
        }
    }
    for uid in ("super-user", "normal-user", "peer-user"):
        prompt = PromptLibrary(
            uid=uid,
            name=f"permission-{uid}-prompt",
            content=f"{uid} prompt content",
        )
        session.add(prompt)
        await session.flush()
        profile = Profile(
            uid=uid,
            name=f"permission-{uid}-profile",
            prompt_id=prompt.id,
            configs={"permission_fixture": uid},
            is_default=True,
        )
        session.add(profile)
        await session.flush()
        assert profile.id is not None

        user_knowledge_base = _knowledge_base(
            uid=uid,
            name="user",
            channel_id=source_channel.id,
            knowledge_base_type=KnowledgeBaseType.USER,
            managed_profile_id=None,
        )
        managed_knowledge_base = _knowledge_base(
            uid=uid,
            name="managed",
            channel_id=source_channel.id,
            knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
            managed_profile_id=profile.id,
        )
        session.add_all([user_knowledge_base, managed_knowledge_base])
        await session.flush()
        assert user_knowledge_base.id is not None
        assert managed_knowledge_base.id is not None

        binding = KnowledgeBaseProfileBinding(
            uid=uid,
            profile_id=profile.id,
            knowledge_base_id=user_knowledge_base.id,
        )
        document = KnowledgeBaseDocument(
            knowledge_base_id=user_knowledge_base.id,
            filename=f"{uid}-document.txt",
            content=f"{uid} document content",
            chunk_size=1000,
            chunk_overlap=100,
            batch_size=16,
            chunk_count=1,
            chunk_ids=[f"{uid}-document-chunk"],
            metadata_={"owner": uid, "fixture": "permission"},
        )
        content = f"{uid} managed content"
        managed_item = ManagedKnowledgeItem(
            uid=uid,
            knowledge_base_id=managed_knowledge_base.id,
            knowledge_key=f"{uid}.managed.key",
            content=content,
            content_token_count=3,
            content_hash=sha256(content.encode("utf-8")).hexdigest(),
            version=1,
            source_type=ManagedKnowledgeSourceType.USER_API,
            created_by=ManagedKnowledgeActorType.USER,
            last_modified_by=ManagedKnowledgeActorType.USER,
            llm_maintainable=False,
            indexed_version=0,
            vector_item_ids=[],
            is_recallable=False,
        )
        session.add_all([binding, document, managed_item])
        await session.flush()
        assert managed_item.id is not None

        failed_publication_job = KnowledgeJob(
            uid=uid,
            operation=KnowledgeJobOperation.MANAGED_UPDATE,
            dedupe_key=f"{uid}:failed-publication",
            request_hash=sha256(f"{uid}:failed-publication".encode()).hexdigest(),
            status=KnowledgeJobStatus.FAILED,
            knowledge_base_id=managed_knowledge_base.id,
            knowledge_id=managed_item.id,
            expected_version=1,
            payload={"content_hash": managed_item.content_hash},
            error="seeded publication failure",
        )
        session.add(failed_publication_job)
        await session.flush()
        assert failed_publication_job.id is not None
        managed_item.source_job_id = failed_publication_job.id

        revision = ManagedKnowledgeRevision(
            uid=uid,
            knowledge_base_id=managed_knowledge_base.id,
            knowledge_id=managed_item.id,
            version=1,
            operation=ManagedKnowledgeRevisionOperation.CREATE,
            before_snapshot=None,
            after_snapshot=build_managed_knowledge_snapshot(managed_item),
            source_type=ManagedKnowledgeSourceType.USER_API,
            source_reference=None,
            source_job_id=failed_publication_job.id,
            modified_by=ManagedKnowledgeActorType.USER,
        )
        organization_job = KnowledgeJob(
            uid=uid,
            operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
            dedupe_key=f"{uid}:organization",
            request_hash=sha256(f"{uid}:organization".encode()).hexdigest(),
            status=KnowledgeJobStatus.FAILED,
            knowledge_base_id=managed_knowledge_base.id,
            payload={
                "knowledge_base_id": managed_knowledge_base.id,
                "source": "manual",
                "snapshot_nonce": f"{uid}-snapshot",
            },
            error="seeded organization failure",
        )
        session.add_all([revision, organization_job])
        await session.commit()
        assert organization_job.id is not None
        resources[uid] = {
            "profile_id": profile.id,
            "user_kb_id": user_knowledge_base.id,
            "managed_kb_id": managed_knowledge_base.id,
            "document_id": document.id,
            "managed_item_id": managed_item.id,
            "failed_publication_job_id": failed_publication_job.id,
            "organization_job_id": organization_job.id,
            "managed_knowledge_key": managed_item.knowledge_key,
            "document_content": document.content,
            "managed_content": managed_item.content,
        }

    return resources


@pytest_asyncio.fixture
async def seeded_database(database_factory, password_hash: str) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], dict[str, Any]]]:
    async with database_factory(tables=_TABLES, foreign_keys=True, name="knowledge-base-permissions.sqlite3") as session_factory:
        async with session_factory() as session:
            resources = await _seed_database(session, password_hash)
        yield session_factory, resources


@pytest_asyncio.fixture
async def api_app(
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    session_factory, _resources = seeded_database
    monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(knowledge_base.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


def _freeze(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


async def _state_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[Any, ...]:
    async with session_factory() as session:
        snapshots = []
        for model in _SNAPSHOT_MODELS:
            rows = (await session.execute(select(model).order_by(*model.__table__.primary_key.columns))).scalars().all()
            attributes = tuple(model.__mapper__.column_attrs)
            snapshots.append(tuple(tuple(_freeze(getattr(row, attribute.key)) for attribute in attributes) for row in rows))
        return tuple(snapshots)


async def _get_knowledge_base(
    session_factory: async_sessionmaker[AsyncSession],
    knowledge_base_id: int,
) -> KnowledgeBase | None:
    async with session_factory() as session:
        return await session.get(KnowledgeBase, knowledge_base_id)


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
    files: Any = None,
    data: Any = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.request(
        method,
        path,
        headers=headers,
        json=json,
        params=params,
        files=files,
        data=data,
    )


def _assert_error(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == message
    assert payload["data"] is None


def _assert_success(response: httpx.Response) -> Any:
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    return payload["data"]


def _management_cases() -> tuple[dict[str, str | None], ...]:
    cases: list[dict[str, str | None]] = [
        {"id": "list", "kind": "list", "scope": None},
        {"id": "create", "kind": "create", "scope": None},
    ]
    scoped_kinds = (
        "update",
        "delete",
        "embedding-migration",
        "query-test",
        "profile-bindings-get",
        "profile-bindings-post",
        "documents-list",
        "documents-get",
        "documents-import",
        "documents-delete",
        "managed-items-list",
        "managed-items-get",
        "managed-items-history",
        "managed-items-create",
        "managed-items-update",
        "managed-items-delete",
        "managed-items-retry",
        "organization",
        "organization-jobs",
        "organization-get",
        "organization-cancel",
        "organization-retry",
    )
    cases.extend({"id": f"{kind}-{scope}", "kind": kind, "scope": scope} for kind in scoped_kinds for scope in ("own", "peer"))
    return tuple(cases)


async def _submit_management_case(
    client: httpx.AsyncClient,
    case: dict[str, str | None],
    resources: dict[str, Any],
) -> httpx.Response:
    kind = case["kind"]
    scope = case["scope"]
    target = resources["normal-user" if scope == "own" else "peer-user"] if scope else None
    token = _token("normal-user")
    source_channel_id = resources["channels"]["source_id"]
    target_channel_id = resources["channels"]["target_id"]

    if kind == "list":
        return await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=token)
    if kind == "create":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/create",
            token=token,
            json={
                "name": "permission-created",
                "description": "permission create request",
                "embedding_channel_id": source_channel_id,
                "embedding_model_id": "embed-v1",
            },
        )

    assert target is not None
    if kind == "update":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/update",
            token=token,
            params={"kb_id": target["user_kb_id"]},
            json={"name": "permission-updated", "description": "permission update request"},
        )
    if kind == "delete":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/delete",
            token=token,
            params={"kb_id": target["user_kb_id"]},
        )
    if kind == "embedding-migration":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/embedding-migration",
            token=token,
            params={"kb_id": target["user_kb_id"]},
            json={"embedding_channel_id": target_channel_id, "embedding_model_id": "embed-v2"},
        )
    if kind == "query-test":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/query-test",
            token=token,
            params={"kb_id": target["user_kb_id"]},
            json={"query": "permission query", "top_k": 1},
        )
    if kind == "profile-bindings-get":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/profile-bindings",
            token=token,
            params={"profile_id": target["profile_id"]},
        )
    if kind == "profile-bindings-post":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/profile-bindings",
            token=token,
            params={"profile_id": target["profile_id"]},
            json={"knowledge_base_ids": [target["user_kb_id"]]},
        )
    if kind == "documents-list":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/list",
            token=token,
            params={"kb_id": target["user_kb_id"], "page": 1, "size": 20},
        )
    if kind == "documents-get":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/get",
            token=token,
            params={"kb_id": target["user_kb_id"], "document_id": target["document_id"]},
        )
    if kind == "documents-import":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/import",
            token=token,
            params={"kb_id": target["user_kb_id"]},
            files={"file": ("permission-import.txt", b"permission import content", "text/plain")},
            data={"chunk_size": "1000", "chunk_overlap": "100", "batch_size": "16"},
        )
    if kind == "documents-delete":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/delete",
            token=token,
            params={"kb_id": target["user_kb_id"], "document_id": target["document_id"]},
        )
    if kind == "managed-items-list":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/list",
            token=token,
            params={"kb_id": target["managed_kb_id"], "page": 1, "size": 20, "query": "managed"},
        )
    if kind == "managed-items-get":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/get",
            token=token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
        )
    if kind == "managed-items-history":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/history",
            token=token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
        )
    if kind == "managed-items-create":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/create",
            token=token,
            params={"kb_id": target["managed_kb_id"]},
            json={
                "knowledge_key": "permission.new.key",
                "content": "permission managed create content",
                "llm_maintainable": False,
                "dedupe_key": f"permission-managed-create-{scope}",
            },
        )
    if kind == "managed-items-update":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/update",
            token=token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
            json={
                "knowledge_key": target["managed_knowledge_key"],
                "content": "permission managed update content",
                "expected_version": 1,
                "llm_maintainable": False,
                "dedupe_key": f"permission-managed-update-{scope}",
            },
        )
    if kind == "managed-items-delete":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/delete",
            token=token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
            json={"expected_version": 1, "dedupe_key": f"permission-managed-delete-{scope}"},
        )
    if kind == "managed-items-retry":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/retry",
            token=token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
            json={
                "expected_version": 1,
                "failed_job_id": target["failed_publication_job_id"],
                "dedupe_key": f"permission-managed-retry-{scope}",
            },
        )
    if kind == "organization":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/organization",
            token=token,
            params={"kb_id": target["managed_kb_id"]},
            json={
                "dedupe_key": f"permission-organization-{scope}",
            },
        )
    if kind == "organization-jobs":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/organization/jobs",
            token=token,
            params={"kb_id": target["managed_kb_id"], "page": 1, "size": 20},
        )
    if kind == "organization-get":
        return await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/organization/{target['organization_job_id']}",
            token=token,
            params={"kb_id": target["managed_kb_id"]},
        )
    if kind == "organization-cancel":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/organization/{target['organization_job_id']}/cancel",
            token=token,
            params={"kb_id": target["managed_kb_id"]},
        )
    if kind == "organization-retry":
        return await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/organization/{target['organization_job_id']}/retry",
            token=token,
            params={"kb_id": target["managed_kb_id"]},
        )
    raise AssertionError(f"unknown management case: {kind}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (case for case in _management_cases() if case["scope"] == "peer"),
    ids=lambda case: case["id"],
)
async def test_non_admin_cross_owner_management_requests_are_forbidden_and_persistence_is_unchanged(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    case: dict[str, str | None],
) -> None:
    session_factory, resources = seeded_database
    before = await _state_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _submit_management_case(client, case, resources)
    _assert_error(response, 403, t(ERR_SESSION_NO_PERMISSION))
    assert await _state_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_signed_token_cannot_forge_superuser_or_uid_claims(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
) -> None:
    session_factory, resources = seeded_database
    normal = resources["normal-user"]
    peer = resources["peer-user"]
    token = _token("normal-user", is_superuser=True, uid="super-user")
    before = await _state_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=token)
        listed = _assert_success(list_response)
        assert listed["total"] == 2
        assert all(item["uid"] == "normal-user" for item in listed["items"])
        assert {item["id"] for item in listed["items"]} == {normal["user_kb_id"], normal["managed_kb_id"]}

        update_response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/update",
            token=token,
            params={"kb_id": peer["user_kb_id"]},
            json={"name": "forged metadata update", "description": "must be forbidden"},
        )
    _assert_error(update_response, 403, t(ERR_SESSION_NO_PERMISSION))
    assert await _state_snapshot(session_factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ("missing", "invalid", "expired", "disabled", "unknown"))
async def test_list_rejects_missing_invalid_expired_disabled_and_unknown_credentials(
    api_app: FastAPI,
    credential: str,
) -> None:
    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = security.create_access_token(data={"sub": "normal-user"}, expires_delta=timedelta(seconds=-1))
    elif credential == "disabled":
        token = _token("disabled-user")
    else:
        token = _token("does-not-exist")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=token)
    _assert_error(response, 401, t(ERR_UNAUTHORIZED))


@pytest.mark.asyncio
@pytest.mark.parametrize("is_superuser", (False, True), ids=("normal-user", "super-admin"))
async def test_users_read_allowed_resources_and_persist_metadata_update(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    is_superuser: bool,
) -> None:
    session_factory, resources = seeded_database
    expected_usernames_by_uid = {
        "super-user": "super-admin",
        "normal-user": "normal-user",
        "peer-user": "peer-user",
    }
    if is_superuser:
        auth_token = _token("super-admin")
        target_owner_uid = "peer-user"
        listed_owner_uids = ("super-user", "normal-user", "peer-user")
        expected_total = 6
    else:
        auth_token = _token("normal-user")
        target_owner_uid = "normal-user"
        listed_owner_uids = ("normal-user",)
        expected_total = 2
    target = resources[target_owner_uid]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        listed_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=auth_token)
        listed = _assert_success(listed_response)
        listed_pairs = {(item["uid"], item["knowledge_base_type"]) for item in listed["items"]}
        assert listed["total"] == expected_total
        assert listed_pairs == {(uid, kind) for uid in listed_owner_uids for kind in (KnowledgeBaseType.USER.value, KnowledgeBaseType.LLM_MANAGED.value)}
        assert {(item["uid"], item["username"]) for item in listed["items"]} == {(uid, expected_usernames_by_uid[uid]) for uid in listed_owner_uids}

        document_list_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/list",
            token=auth_token,
            params={"kb_id": target["user_kb_id"], "page": 1, "size": 20},
        )
        document_list = _assert_success(document_list_response)
        assert document_list["total"] == 1
        assert document_list["items"][0]["id"] == target["document_id"]

        document_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/documents/get",
            token=auth_token,
            params={"kb_id": target["user_kb_id"], "document_id": target["document_id"]},
        )
        document = _assert_success(document_response)
        assert document["id"] == target["document_id"]
        assert document["content"] == target["document_content"]

        managed_list_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/list",
            token=auth_token,
            params={"kb_id": target["managed_kb_id"], "page": 1, "size": 20},
        )
        managed_list = _assert_success(managed_list_response)
        assert managed_list["total"] == 1
        assert managed_list["items"][0]["id"] == target["managed_item_id"]

        managed_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/get",
            token=auth_token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
        )
        managed = _assert_success(managed_response)
        assert managed["id"] == target["managed_item_id"]
        assert managed["knowledge_base_id"] == target["managed_kb_id"]
        assert managed["content"] == target["managed_content"]

        history_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/history",
            token=auth_token,
            params={"kb_id": target["managed_kb_id"], "knowledge_id": target["managed_item_id"]},
        )
        history = _assert_success(history_response)
        assert [(entry["version"], entry["operation"]) for entry in history] == [(1, "create")]

        updated_response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/update",
            token=auth_token,
            params={"kb_id": target["user_kb_id"]},
            json={"name": "peer metadata updated", "description": "updated by superuser"},
        )
        updated = _assert_success(updated_response)
        assert updated["uid"] == target_owner_uid
        assert updated["username"] == expected_usernames_by_uid[target_owner_uid]
        assert updated["name"] == "peer metadata updated"
        assert updated["description"] == "updated by superuser"

    persisted = await _get_knowledge_base(session_factory, target["user_kb_id"])
    assert persisted is not None
    assert persisted.id == target["user_kb_id"]
    assert persisted.uid == target_owner_uid
    assert persisted.name == "peer metadata updated"
    assert persisted.description == "updated by superuser"
    assert persisted.knowledge_base_type == KnowledgeBaseType.USER
    assert persisted.collection_name == f"permission-{target_owner_uid}-user-collection"


@pytest.mark.asyncio
async def test_knowledge_base_owner_username_refreshes_after_real_user_rename(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
) -> None:
    session_factory, resources = seeded_database
    peer = resources["peer-user"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        initial_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=_token("super-admin"))
        initial_list = _assert_success(initial_list_response)
        initial_peer_items = [item for item in initial_list["items"] if item["uid"] == "peer-user"]
        assert {item["id"] for item in initial_peer_items} == {peer["user_kb_id"], peer["managed_kb_id"]}
        assert {(item["uid"], item["username"]) for item in initial_peer_items} == {("peer-user", "peer-user")}

    async with session_factory() as session:
        peer_user = (await session.execute(select(User).where(User.uid == "peer-user"))).scalar_one()
        assert peer_user.username == "peer-user"
        peer_user.username = "peer-renamed"
        await session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        renamed_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=_token("super-admin"))
        renamed_list = _assert_success(renamed_list_response)
        renamed_peer_items = [item for item in renamed_list["items"] if item["uid"] == "peer-user"]
        assert {item["id"] for item in renamed_peer_items} == {peer["user_kb_id"], peer["managed_kb_id"]}
        assert {(item["uid"], item["username"]) for item in renamed_peer_items} == {("peer-user", "peer-renamed")}

        updated_response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/update",
            token=_token("super-admin"),
            params={"kb_id": peer["user_kb_id"]},
            json={"name": "peer renamed metadata", "description": "updated after owner rename"},
        )
        updated = _assert_success(updated_response)
        assert updated["uid"] == "peer-user"
        assert updated["username"] == "peer-renamed"

        normal_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=_token("normal-user"))
        normal_list = _assert_success(normal_list_response)
        assert normal_list["total"] == 2
        assert {item["id"] for item in normal_list["items"]} == {
            resources["normal-user"]["user_kb_id"],
            resources["normal-user"]["managed_kb_id"],
        }
        assert {(item["uid"], item["username"]) for item in normal_list["items"]} == {("normal-user", "normal-user")}


@pytest.mark.asyncio
async def test_same_real_user_can_create_managed_knowledge_over_http_and_llm_tool(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
) -> None:
    session_factory, resources = seeded_database
    token = _token("normal-user")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        me_response = await client.get("/api/v1/auth/me", headers=headers)
    me = _assert_success(me_response)
    assert me["is_superuser"] is False
    uid = me["uid"]
    assert uid == "normal-user"

    normal = resources[uid]
    async with session_factory() as session:
        profile = await session.get(Profile, normal["profile_id"])
        managed_knowledge_base = await session.get(KnowledgeBase, normal["managed_kb_id"])
        assert profile is not None
        assert managed_knowledge_base is not None
        assert profile.uid == uid
        assert managed_knowledge_base.uid == uid
        assert managed_knowledge_base.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED
        assert managed_knowledge_base.managed_profile_id == profile.id

        profile_config = ProfileConfig.model_validate(
            {
                "channel": {},
                "security": {},
                "tool": {},
                "other": {},
                "memory": {"enabled": True},
            }
        )
        profile.configs = profile_config.model_dump(mode="json")
        await session.commit()
        await session.refresh(profile)

    http_content = "Management-created managed knowledge."
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        http_response = await client.post(
            f"{KNOWLEDGE_BASE_PREFIX}/managed-items/create",
            headers=headers,
            params={"kb_id": normal["managed_kb_id"]},
            json={
                "knowledge_key": "management.allowed",
                "content": http_content,
                "llm_maintainable": True,
                "dedupe_key": "management-allowed-create",
            },
        )
    http_payload = _assert_success(http_response)
    http_item_payload = http_payload["item"]
    http_job_id = http_payload["job_id"]
    assert http_payload["status"] == "created"
    assert http_item_payload["id"] > 0
    assert http_item_payload["knowledge_base_id"] == normal["managed_kb_id"]
    assert http_item_payload["knowledge_key"] == "management.allowed"
    assert http_item_payload["content"] == http_content
    assert http_item_payload["source_type"] == ManagedKnowledgeSourceType.USER_API.value
    assert http_item_payload["created_by"] == ManagedKnowledgeActorType.USER.value
    assert http_item_payload["last_modified_by"] == ManagedKnowledgeActorType.USER.value
    assert http_job_id > 0

    async with session_factory() as session:
        http_item = await session.get(ManagedKnowledgeItem, http_item_payload["id"])
        http_job = await session.get(KnowledgeJob, http_job_id)
        assert http_item is not None
        assert http_item.uid == uid
        assert http_item.knowledge_base_id == normal["managed_kb_id"]
        assert http_item.knowledge_key == "management.allowed"
        assert http_item.content == http_content
        assert http_item.source_type == ManagedKnowledgeSourceType.USER_API
        assert http_item.created_by == ManagedKnowledgeActorType.USER
        assert http_item.last_modified_by == ManagedKnowledgeActorType.USER
        assert http_item.source_job_id == http_job_id

        assert http_job is not None
        assert http_job.uid == uid
        assert http_job.operation == KnowledgeJobOperation.MANAGED_CREATE
        assert http_job.status == KnowledgeJobStatus.PENDING
        assert http_job.knowledge_base_id == normal["managed_kb_id"]
        assert http_job.knowledge_id == http_item.id
        assert http_job.source_profile_id == normal["profile_id"]

    knowledge_content = "Conversation-created managed knowledge."
    async with session_factory() as session:
        profile = await session.get(Profile, normal["profile_id"])
        assert profile is not None
        dispatch_context = build_dispatch_context(
            mode="interactive",
            source="interactive_tool",
            uid=uid,
            session_id="permission-conversation-session",
            profile=profile,
            db=session,
            tool_call_id="permission-conversation-tool-call",
            source_message_id=707,
        )
        executor = LongTermMemoryExecutor(project_root=".", uid=uid)
        executor.set_config(ProfileConfig.model_validate(profile.configs))
        executor.set_runtime_context(dispatch_context=dispatch_context)
        tool_payload = json.loads(
            await executor.execute(
                operation="knowledge_create",
                knowledge_key="conversation.allowed",
                knowledge_content=knowledge_content,
            )
        )

    assert tool_payload["status"] == "accepted"
    assert tool_payload["mutation_status"] == "created"
    assert tool_payload["knowledge_base_created"] is False
    assert tool_payload["knowledge_id"] > 0
    assert tool_payload["job_id"] > 0
    assert tool_payload["current_version"] == 1

    async with session_factory() as session:
        stored_item = await session.get(ManagedKnowledgeItem, tool_payload["knowledge_id"])
        stored_job = await session.get(KnowledgeJob, tool_payload["job_id"])
        assert stored_item is not None
        assert stored_item.uid == uid
        assert stored_item.knowledge_base_id == normal["managed_kb_id"]
        assert stored_item.content == knowledge_content
        assert stored_item.source_type == ManagedKnowledgeSourceType.LLM_TOOL
        assert stored_item.created_by == ManagedKnowledgeActorType.LLM
        assert stored_item.last_modified_by == ManagedKnowledgeActorType.LLM
        assert stored_item.llm_maintainable is True
        assert stored_item.version == 1

        assert stored_job is not None
        assert stored_job.uid == uid
        assert stored_job.operation == KnowledgeJobOperation.MANAGED_CREATE
        assert stored_job.status == KnowledgeJobStatus.PENDING
        assert stored_job.knowledge_base_id == normal["managed_kb_id"]
        assert stored_job.knowledge_id == stored_item.id
        assert stored_job.source_profile_id == normal["profile_id"]
        assert stored_job.source_session_id == "permission-conversation-session"
        assert stored_job.source_message_id == 707


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_fields",
    (
        {},
        {"uid": None},
        {"uid": "peer-user", "username": "peer-user", "is_superuser": True},
        {"uid": "missing-owner", "username": "super-admin", "is_superuser": True},
    ),
)
async def test_normal_user_create_knowledge_base_uses_authenticated_owner_and_scope_lists(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    owner_fields: dict[str, Any],
) -> None:
    session_factory, resources = seeded_database
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    async def skip_external_collection(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(knowledge_base, "async_create_collection", skip_external_collection)

    peer_before = await _get_knowledge_base(session_factory, resources["peer-user"]["user_kb_id"])
    assert peer_before is not None
    peer_before_state = tuple(_freeze(getattr(peer_before, attribute.key)) for attribute in KnowledgeBase.__mapper__.column_attrs)

    created_name = "normal-user-created"
    created_description = "created by the authenticated normal user"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        login_response = await _request(
            client,
            "POST",
            "/api/v1/auth/login",
            json={"username": "normal-user", "password": TEST_PASSWORD},
        )
        login = _assert_success(login_response)
        normal_token = login["access_token"]

        create_response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/create",
            token=normal_token,
            json={
                "name": created_name,
                "description": created_description,
                "embedding_channel_id": resources["channels"]["source_id"],
                "embedding_model_id": "embed-v1",
                **owner_fields,
            },
        )
        created = _assert_success(create_response)
        assert created["uid"] == "normal-user"
        assert created["username"] == "normal-user"
        assert created["knowledge_base_type"] == KnowledgeBaseType.USER.value
        assert created["name"] == created_name
        assert created["description"] == created_description
        created_id = created["id"]

        normal_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=normal_token)
        normal_list = _assert_success(normal_list_response)
        normal_items = normal_list["items"]
        assert normal_list["total"] == 3
        assert all(item["uid"] == "normal-user" for item in normal_items)
        assert {item["id"] for item in normal_items} == {
            resources["normal-user"]["user_kb_id"],
            resources["normal-user"]["managed_kb_id"],
            created_id,
        }
        assert any(item["id"] == created_id and item["name"] == created_name for item in normal_items)

        super_login_response = await _request(
            client,
            "POST",
            "/api/v1/auth/login",
            json={"username": "super-admin", "password": TEST_PASSWORD},
        )
        super_login = _assert_success(super_login_response)
        super_list_response = await _request(
            client,
            "GET",
            f"{KNOWLEDGE_BASE_PREFIX}/list",
            token=super_login["access_token"],
        )
        super_list = _assert_success(super_list_response)
        seeded_ids = {resources[uid][knowledge_base_key] for uid in ("super-user", "normal-user", "peer-user") for knowledge_base_key in ("user_kb_id", "managed_kb_id")}
        assert super_list["total"] == 7
        assert {item["id"] for item in super_list["items"]} == seeded_ids | {created_id}
        assert {item["uid"] for item in super_list["items"]} == {"super-user", "normal-user", "peer-user"}
        assert any(item["id"] == created_id for item in super_list["items"])

    new_knowledge_base = await _get_knowledge_base(session_factory, created_id)
    assert new_knowledge_base is not None
    assert new_knowledge_base.uid == "normal-user"
    assert new_knowledge_base.knowledge_base_type == KnowledgeBaseType.USER
    assert new_knowledge_base.name == created_name
    assert new_knowledge_base.description == created_description

    peer_after = await _get_knowledge_base(session_factory, resources["peer-user"]["user_kb_id"])
    assert peer_after is not None
    peer_after_state = tuple(_freeze(getattr(peer_after, attribute.key)) for attribute in KnowledgeBase.__mapper__.column_attrs)
    assert peer_after_state == peer_before_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_fields", "expected_uid", "expected_username"),
    (
        ({}, "super-user", "super-admin"),
        ({"uid": None}, "super-user", "super-admin"),
        ({"uid": "super-user"}, "super-user", "super-admin"),
        ({"uid": "peer-user"}, "peer-user", "peer-user"),
        ({"uid": "disabled-user"}, "disabled-user", "disabled-user"),
    ),
    ids=("omitted-owner", "null-owner", "super-owner", "peer-owner", "disabled-owner"),
)
async def test_superuser_create_knowledge_base_resolves_owner_and_preserves_scope_lists(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    owner_fields: dict[str, Any],
    expected_uid: str,
    expected_username: str,
) -> None:
    session_factory, resources = seeded_database
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    async def skip_external_collection(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(knowledge_base, "async_create_collection", skip_external_collection)

    user_attributes = tuple(User.__mapper__.column_attrs)

    async def user_snapshot() -> tuple[tuple[Any, ...], ...]:
        async with session_factory() as session:
            users = (await session.execute(select(User).order_by(User.uid))).scalars().all()
            return tuple(tuple(_freeze(getattr(user, attribute.key)) for attribute in user_attributes) for user in users)

    users_before = await user_snapshot()
    created_name = "admin-created-owner"
    created_description = "created by the superuser for owner resolution"
    super_token = _token("super-admin")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        create_response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/create",
            token=super_token,
            json={
                "name": created_name,
                "description": created_description,
                "embedding_channel_id": resources["channels"]["source_id"],
                "embedding_model_id": "embed-v1",
                **owner_fields,
                "username": "forged-owner-name",
            },
        )
        created = _assert_success(create_response)
        assert created["uid"] == expected_uid
        assert created["username"] == expected_username
        assert created["username"] != "forged-owner-name"
        assert created["knowledge_base_type"] == KnowledgeBaseType.USER.value
        assert created["managed_profile_id"] is None
        created_id = created["id"]

        normal_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=_token("normal-user"))
        normal_list = _assert_success(normal_list_response)
        normal_ids = {resources["normal-user"]["user_kb_id"], resources["normal-user"]["managed_kb_id"]}
        assert normal_list["total"] == 2
        assert {item["id"] for item in normal_list["items"]} == normal_ids
        assert created_id not in {item["id"] for item in normal_list["items"]}
        assert all(item["uid"] == "normal-user" and item["username"] == "normal-user" for item in normal_list["items"])

        peer_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=_token("peer-user"))
        peer_list = _assert_success(peer_list_response)
        peer_ids = {resources["peer-user"]["user_kb_id"], resources["peer-user"]["managed_kb_id"]}
        if expected_uid == "peer-user":
            peer_ids.add(created_id)
        assert peer_list["total"] == len(peer_ids)
        assert {item["id"] for item in peer_list["items"]} == peer_ids
        assert (created_id in peer_ids) is (expected_uid == "peer-user")
        assert all(item["uid"] == "peer-user" and item["username"] == "peer-user" for item in peer_list["items"])

        super_list_response = await _request(client, "GET", f"{KNOWLEDGE_BASE_PREFIX}/list", token=super_token)
        super_list = _assert_success(super_list_response)
        seeded_ids = {resources[uid][knowledge_base_key] for uid in ("super-user", "normal-user", "peer-user") for knowledge_base_key in ("user_kb_id", "managed_kb_id")}
        assert super_list["total"] == 7
        assert {item["id"] for item in super_list["items"]} == seeded_ids | {created_id}
        created_items = [item for item in super_list["items"] if item["id"] == created_id]
        assert len(created_items) == 1
        assert created_items[0]["uid"] == expected_uid
        assert created_items[0]["username"] == expected_username
        assert created_items[0]["username"] != "forged-owner-name"

    new_knowledge_base = await _get_knowledge_base(session_factory, created_id)
    assert new_knowledge_base is not None
    assert new_knowledge_base.uid == expected_uid
    assert new_knowledge_base.name == created_name
    assert new_knowledge_base.knowledge_base_type == KnowledgeBaseType.USER
    assert new_knowledge_base.managed_profile_id is None
    assert await user_snapshot() == users_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_uid", "expected_status"),
    (
        ("missing-owner", 404),
        ("   ", 404),
        ("", 422),
        (True, 422),
        (123, 422),
        ([], 422),
        ({}, 422),
        ("x" * 51, 422),
    ),
    ids=(
        "missing-owner",
        "whitespace-owner",
        "empty-owner",
        "boolean-owner",
        "integer-owner",
        "list-owner",
        "object-owner",
        "overlong-owner",
    ),
)
async def test_superuser_create_knowledge_base_rejects_invalid_owner_uid_without_side_effects(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    owner_uid: Any,
    expected_status: int,
) -> None:
    session_factory, resources = seeded_database
    collection_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def record_external_collection(*args: object, **kwargs: object) -> None:
        collection_calls.append((args, kwargs))

    monkeypatch.setattr(knowledge_base, "async_create_collection", record_external_collection)
    before = await _state_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/create",
            token=_token("super-admin"),
            json={
                "name": "invalid-owner-create",
                "description": "invalid owner request",
                "embedding_channel_id": resources["channels"]["source_id"],
                "embedding_model_id": "embed-v1",
                "uid": owner_uid,
            },
        )

    if expected_status == 404:
        _assert_error(response, 404, t(ERR_USER_NOT_FOUND))
    else:
        payload = response.json()
        assert response.status_code == 422
        assert payload["code"] == 422
        assert payload["data"] is None
        assert "uid" in payload["message"]

    assert await _state_snapshot(session_factory) == before
    assert collection_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "kind"),
    (
        ("ordinary", "user"),
        ("ordinary", "managed"),
        ("super-admin", "user"),
        ("super-admin", "managed"),
    ),
    ids=("ordinary-user", "ordinary-managed", "super-admin-user", "super-admin-managed"),
)
async def test_delete_allowed_scope_removes_only_target_knowledge_base_and_queues_collections(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    role: str,
    kind: str,
) -> None:
    session_factory, resources = seeded_database
    requester_uid = "normal-user" if role == "ordinary" else "super-admin"
    owner_uid = "normal-user" if role == "ordinary" else "peer-user"
    target = resources[owner_uid]
    target_kb_id = target[f"{kind}_kb_id"]

    def _rows_state(model: Any, rows: list[Any]) -> dict[Any, tuple[Any, ...]]:
        primary_key = next(iter(model.__table__.primary_key.columns))
        attributes = tuple(model.__mapper__.column_attrs)
        return {getattr(row, primary_key.key): tuple(_freeze(getattr(row, attribute.key)) for attribute in attributes) for row in rows}

    owned_model_specs = (
        (KnowledgeBaseProfileBinding, KnowledgeBaseProfileBinding.knowledge_base_id),
        (KnowledgeBaseDocument, KnowledgeBaseDocument.knowledge_base_id),
        (KnowledgeBaseCollectionOwner, KnowledgeBaseCollectionOwner.knowledge_base_id),
        (ManagedKnowledgeItem, ManagedKnowledgeItem.knowledge_base_id),
        (ManagedKnowledgeRevision, ManagedKnowledgeRevision.knowledge_base_id),
        (KnowledgeJob, KnowledgeJob.knowledge_base_id),
    )

    async with session_factory() as session:
        knowledge_bases = list((await session.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all())
        target_knowledge_base = next(knowledge_base for knowledge_base in knowledge_bases if knowledge_base.id == target_kb_id)
        non_target_kb_ids = {knowledge_base.id for knowledge_base in knowledge_bases if knowledge_base.id != target_kb_id}
        non_target_kb_states = _rows_state(KnowledgeBase, [knowledge_base for knowledge_base in knowledge_bases if knowledge_base.id in non_target_kb_ids])
        target_collection_names = tuple(name for name in (target_knowledge_base.collection_name, target_knowledge_base.active_collection_name) if name)
        non_target_owned_states = {}
        for model, owner_column in owned_model_specs:
            rows = list((await session.execute(select(model).where(owner_column.in_(non_target_kb_ids)))).scalars().all())
            non_target_owned_states[model] = _rows_state(model, rows)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/delete",
            token=_token(requester_uid),
            params={"kb_id": target_kb_id},
        )
    assert _assert_success(response) is True

    async with session_factory() as session:
        assert await session.get(KnowledgeBase, target_kb_id) is None

        remaining_knowledge_bases = list((await session.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all())
        assert _rows_state(KnowledgeBase, remaining_knowledge_bases) == non_target_kb_states
        for model, owner_column in owned_model_specs:
            rows = list((await session.execute(select(model).where(owner_column.in_(non_target_kb_ids)))).scalars().all())
            assert _rows_state(model, rows) == non_target_owned_states[model]

        profile = await session.get(Profile, target["profile_id"])
        assert profile is not None
        assert profile.uid == owner_uid

        if kind == "user":
            assert await session.get(KnowledgeBaseDocument, target["document_id"]) is None
        else:
            assert await session.get(ManagedKnowledgeItem, target["managed_item_id"]) is None

        owners = list((await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_(target_collection_names)))).scalars().all())
        assert {owner.collection_name for owner in owners} == set(target_collection_names)
        assert all(owner.knowledge_base_id is None for owner in owners)


@pytest.mark.asyncio
async def test_superuser_create_rejects_owner_deleted_before_knowledge_base_write_and_cleans_collection(
    api_app: FastAPI,
    seeded_database: tuple[async_sessionmaker[AsyncSession], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, resources = seeded_database
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    collections: set[str] = set()
    deleted_owner_baseline: tuple[Any, ...] | None = None

    async def create_collection(name: str) -> None:
        nonlocal deleted_owner_baseline
        collections.add(name)

        async with session_factory() as session:
            owner = (await session.execute(select(User).where(User.uid == "disabled-user"))).scalar_one()
            await session.delete(owner)
            await session.commit()
        deleted_owner_baseline = await _state_snapshot(session_factory)

    async def delete_collection(name: str) -> None:
        collections.discard(name)

    monkeypatch.setattr(knowledge_base, "async_create_collection", create_collection)
    monkeypatch.setattr(knowledge_base, "async_delete_collection", delete_collection)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            f"{KNOWLEDGE_BASE_PREFIX}/create",
            token=_token("super-admin"),
            json={
                "name": "deleted-owner-race",
                "description": "owner deleted after prevalidation",
                "embedding_channel_id": resources["channels"]["source_id"],
                "embedding_model_id": "embed-v1",
                "uid": "disabled-user",
            },
        )

    _assert_error(response, 404, t(ERR_USER_NOT_FOUND))
    assert deleted_owner_baseline is not None
    assert await _state_snapshot(session_factory) == deleted_owner_baseline
    assert collections == set()

    async with session_factory() as session:
        missing_owner = (await session.execute(select(User).where(User.uid == "disabled-user"))).scalar_one_or_none()
        assert missing_owner is None
