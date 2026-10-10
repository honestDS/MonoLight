from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.crypto as crypto_module
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.memory.normalization import build_memory_content_hash
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import (
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemorySource,
    LongTermMemoryType,
)
from app.models.user import User
from tests.integration.test_memory_management_workflow import API_TABLES as WORKFLOW_API_TABLES
from tests.integration.test_memory_management_workflow import (
    _assert_page,
    _assert_standard,
    _chat_model,
    _create_chat_channel,
    _create_job,
    _create_record,
    _create_store,
    _migration_payload,
    _publication_payload,
)
from tests.integration.test_memory_management_workflow import api_app as _workflow_api_app

api_app = _workflow_api_app
API_TABLES = [*WORKFLOW_API_TABLES, User.__table__]


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(tables=API_TABLES, name=f"memory-permissions-{uuid4().hex}.sqlite3") as session_factory:
        async with session_factory() as session:
            session.add_all(
                [
                    User(uid="user-a", username="user-a", is_superuser=False),
                    User(uid="user-b", username="user-b", is_superuser=False),
                ]
            )
            await session.commit()
            yield session


def _memory_request(*, dedupe_key: str, memory_key: str, content: str) -> dict[str, object]:
    return {
        "dedupe_key": dedupe_key,
        "content": content,
        "memory_key": memory_key,
        "memory_type": LongTermMemoryType.FACT.value,
    }


async def _create_history_revision(
    db: AsyncSession,
    *,
    uid: str,
    memory_id: int,
    memory_key: str,
    content: str,
    version: int = 1,
    source_job_id: int | None = None,
) -> None:
    await memory_revision_crud.create(
        db,
        uid=uid,
        memory_id=memory_id,
        version=version,
        memory_key=memory_key,
        memory_type=LongTermMemoryType.FACT,
        content=content,
        content_token_count=estimate_tokens(content),
        content_hash=build_memory_content_hash(content),
        source=LongTermMemorySource.USER_API,
        source_job_id=source_job_id,
    )


def _migration_payload_for(uid: str) -> dict:
    payload = _migration_payload()
    payload["from"]["collection"] = f"collection-{uid}"
    payload["target"]["collection"] = f"collection-{uid}-v2"
    return payload


async def _create_failed_migration(
    db: AsyncSession,
    *,
    uid: str,
    payload: dict,
    cursor: int,
    total: int,
):
    job = await _create_job(
        db,
        uid=uid,
        dedupe_key=f"migration-{uid}",
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        status=LongTermMemoryMutationStatus.FAILED,
        payload=payload,
        error="migration failed",
        finished_at=datetime.now(UTC),
    )
    assert job.id is not None
    source = payload["from"]
    target = payload["target"]
    revision = await memory_embedding_revision_crud.create(
        db,
        uid=uid,
        revision=1,
        from_channel_id=source["channel_id"],
        from_model_id=source["model_id"],
        from_dimensions=source["dimensions"],
        from_signature=source["signature"],
        from_collection=source["collection"],
        to_channel_id=target["channel_id"],
        to_model_id=target["model_id"],
        to_dimensions=target["dimensions"],
        to_signature=target["signature"],
        to_collection=target["collection"],
        job_id=job.id,
        status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
        error="migration failed",
    )
    store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store is not None
    store.migration_job_id = job.id
    store.migration_status = LongTermMemoryMigrationStatus.FAILED
    store.migration_snapshot_boundary = total
    store.migration_cursor = cursor
    store.migration_total_count = total
    store.migration_success_count = max(cursor - 1, 0)
    store.migration_failure_count = 1
    store.migration_error = "migration failed"
    await db.commit()
    return job, revision


@pytest.mark.asyncio
async def test_memory_management_permissions_scope_list_and_meta(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    await _create_record(db_session, uid="user-a", memory_key="needle-low", content="needle low", version=1)
    await _create_record(
        db_session,
        uid="user-a",
        memory_key="needle-todo",
        content="needle todo",
        version=2,
        memory_type=LongTermMemoryType.TODO,
    )
    high = await _create_record(db_session, uid="user-a", memory_key="needle-high", content="needle high", version=3)
    foreign = await _create_record(db_session, uid="user-b", memory_key="needle-foreign", content="needle foreign", version=4)
    assert high.id is not None and foreign.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        all_memories = _assert_standard(await client.get("/api/v1/memories/list"), 200)
        _assert_page(all_memories, total=4)
        assert {item["owner_uid"] for item in all_memories["data"]["items"]} == {"user-a", "user-b"}
        assert all_memories["data"]["meta"] == {
            "is_superuser": True,
            "current_uid": "user-a",
            "current_username": None,
        }

        filtered = _assert_standard(
            await client.get(
                "/api/v1/memories/list",
                params={
                    "page": 2,
                    "size": 1,
                    "keyword": "needle",
                    "memory_type": LongTermMemoryType.FACT.value,
                    "sort_by": "version",
                    "sort_order": "desc",
                },
            ),
            200,
        )
        _assert_page(filtered, total=3, page=2, size=1)
        assert filtered["data"]["items"][0]["id"] == high.id
        assert filtered["data"]["items"][0]["owner_uid"] == "user-a"

        explicit_foreign = _assert_standard(
            await client.get("/api/v1/memories/list", params={"uid": "user-b", "size": 1}),
            200,
        )
        _assert_page(explicit_foreign, total=1, size=1)
        assert explicit_foreign["data"]["items"][0]["owner_uid"] == "user-b"

        current_user.is_superuser = False
        own_default = _assert_standard(await client.get("/api/v1/memories/list"), 200)
        _assert_page(own_default, total=3)
        assert {item["owner_uid"] for item in own_default["data"]["items"]} == {"user-a"}
        assert own_default["data"]["meta"] == {
            "is_superuser": False,
            "current_uid": "user-a",
            "current_username": None,
        }
        own_explicit = _assert_standard(await client.get("/api/v1/memories/list?uid=user-a"), 200)
        _assert_page(own_explicit, total=3)
        assert {item["owner_uid"] for item in own_explicit["data"]["items"]} == {"user-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "history", "pin", "unpin", "update", "delete", "resume"])
async def test_admin_can_manage_foreign_memory_records_without_cross_owner_mutation(
    operation: str,
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    own = await _create_record(db_session, uid="user-a", memory_key="own-record", content="own content")
    foreign = await _create_record(db_session, uid="user-b", memory_key="foreign-record", content="foreign content")
    assert own.id is not None and foreign.id is not None
    own_before = (own.content, own.version, own.pinned, own.is_active)

    if operation == "history":
        await _create_history_revision(
            db_session,
            uid="user-b",
            memory_id=foreign.id,
            memory_key="foreign-record",
            content="foreign history",
            source_job_id=7001,
        )
    elif operation == "resume":
        failed_job = await _create_job(
            db_session,
            uid="user-b",
            dedupe_key="foreign-resume-failed",
            operation=LongTermMemoryMutationOperation.UPDATE,
            status=LongTermMemoryMutationStatus.FAILED,
            payload=_publication_payload(key="foreign-record", content="failed update"),
            memory_id=foreign.id,
            expected_version=1,
            error="update failed",
            finished_at=datetime.now(UTC),
        )
        assert failed_job.id is not None
        foreign.suppress_recall = True
        foreign.suppressed_by_job_id = failed_job.id
        foreign.pending_mutation_job_id = None
        await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        if operation == "get":
            response = await client.get("/api/v1/memories/get", params={"memory_id": foreign.id})
            data = _assert_standard(response, 200)["data"]
            assert data["owner_uid"] == "user-b"
        elif operation == "history":
            history = _assert_standard(await client.get(f"/api/v1/memories/{foreign.id}/history"), 200)
            _assert_page(history, total=1)
            assert history["data"]["items"][0]["owner_uid"] == "user-b"
        elif operation in {"pin", "unpin"}:
            if operation == "unpin":
                foreign.pinned = True
                await db_session.commit()
            data = _assert_standard(await client.post(f"/api/v1/memories/{foreign.id}/{operation}"), 200)["data"]
            assert data["owner_uid"] == "user-b"
            persisted = await memory_record_crud.get_by_id(db_session, uid="user-b", memory_id=foreign.id)
            assert persisted is not None and persisted.pinned is (operation == "pin")
        elif operation == "update":
            data = _assert_standard(
                await client.post(
                    "/api/v1/memories/update",
                    json={
                        "memory_id": foreign.id,
                        "expected_version": 1,
                        "dedupe_key": "foreign-update",
                        "content": "foreign updated",
                        "memory_key": "foreign-updated",
                        "memory_type": LongTermMemoryType.FACT.value,
                    },
                ),
                200,
            )["data"]
            assert data["job"]["owner_uid"] == "user-b"
            assert data["job"]["operation"] == LongTermMemoryMutationOperation.UPDATE.value
        elif operation == "delete":
            data = _assert_standard(
                await client.post(
                    "/api/v1/memories/delete",
                    json={
                        "memory_id": foreign.id,
                        "expected_version": 1,
                        "dedupe_key": "foreign-delete",
                    },
                ),
                200,
            )["data"]
            assert data["job"]["owner_uid"] == "user-b"
            assert data["job"]["operation"] == LongTermMemoryMutationOperation.DELETE_CLEANUP.value
        else:
            data = _assert_standard(
                await client.post(f"/api/v1/memories/{foreign.id}/resume-current", json={"expected_version": 1}),
                200,
            )["data"]
            assert data["status"] == "resumed"
            assert data["record"]["owner_uid"] == "user-b"

    persisted_foreign = await memory_record_crud.get_by_id(db_session, uid="user-b", memory_id=foreign.id)
    persisted_own = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=own.id)
    assert persisted_foreign is not None and persisted_foreign.uid == "user-b"
    assert persisted_own is not None
    assert (persisted_own.content, persisted_own.version, persisted_own.pinned, persisted_own.is_active) == own_before
    if operation in {"update", "delete"}:
        jobs = await memory_job_crud.get_page(db_session, uid="user-b", skip=0, limit=10)
        assert len(jobs) == 1 and jobs[0].uid == "user-b"
    elif operation == "resume":
        assert persisted_foreign.suppress_recall is False
        assert persisted_foreign.suppressed_by_job_id is None
        assert persisted_foreign.pending_mutation_job_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete", "resume"])
async def test_admin_preserves_memory_version_conflicts(
    operation: str,
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    own = await _create_record(db_session, uid="user-a", memory_key="own-conflict", content="own conflict")
    foreign = await _create_record(db_session, uid="user-b", memory_key="foreign-conflict", content="foreign conflict")
    assert own.id is not None and foreign.id is not None
    own_id = int(own.id)
    foreign_id = int(foreign.id)
    if operation == "resume":
        failed_job = await _create_job(
            db_session,
            uid="user-b",
            dedupe_key="conflict-resume-failed",
            operation=LongTermMemoryMutationOperation.UPDATE,
            status=LongTermMemoryMutationStatus.FAILED,
            payload=_publication_payload(key="foreign-conflict", content="failed conflict update"),
            memory_id=foreign_id,
            expected_version=1,
        )
        assert failed_job.id is not None
        failed_job_id = int(failed_job.id)
        foreign.suppress_recall = True
        foreign.suppressed_by_job_id = failed_job_id
        await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        if operation == "update":
            response = await client.post(
                "/api/v1/memories/update",
                json={
                    "memory_id": foreign_id,
                    "expected_version": 99,
                    "dedupe_key": "foreign-version-conflict-update",
                    "content": "must not update",
                    "memory_key": "must-not-update",
                    "memory_type": LongTermMemoryType.FACT.value,
                },
            )
        elif operation == "delete":
            response = await client.post(
                "/api/v1/memories/delete",
                json={"memory_id": foreign_id, "expected_version": 99, "dedupe_key": "foreign-version-conflict-delete"},
            )
        else:
            response = await client.post(f"/api/v1/memories/{foreign_id}/resume-current", json={"expected_version": 99})
        _assert_standard(response, 409)

    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    b_jobs = await memory_job_crud.count(db_session, uid="user-b")
    assert b_jobs == (1 if operation == "resume" else 0)
    persisted_own = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=own_id)
    persisted_foreign = await memory_record_crud.get_by_id(db_session, uid="user-b", memory_id=foreign_id)
    assert persisted_own is not None and persisted_own.content == "own conflict"
    assert persisted_foreign is not None and persisted_foreign.version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "history", "pin", "unpin", "update", "delete", "resume"])
async def test_regular_user_cannot_manage_foreign_memory_records_or_forge_scope(
    operation: str,
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    own = await _create_record(db_session, uid="user-a", memory_key="regular-own", content="regular own")
    foreign = await _create_record(db_session, uid="user-b", memory_key="regular-foreign", content="regular foreign", pinned=operation == "unpin")
    assert own.id is not None and foreign.id is not None
    own_id = int(own.id)
    foreign_id = int(foreign.id)
    if operation == "history":
        await _create_history_revision(
            db_session,
            uid="user-b",
            memory_id=foreign_id,
            memory_key="regular-foreign",
            content="regular foreign history",
        )
    foreign_before = (foreign.content, foreign.version, foreign.pinned, foreign.suppress_recall)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if operation == "get":
            response = await client.get("/api/v1/memories/get", params={"memory_id": foreign_id})
        elif operation == "history":
            response = await client.get(f"/api/v1/memories/{foreign_id}/history")
        elif operation in {"pin", "unpin"}:
            response = await client.post(f"/api/v1/memories/{foreign_id}/{operation}")
        elif operation == "update":
            response = await client.post(
                "/api/v1/memories/update",
                json={
                    "memory_id": foreign_id,
                    "expected_version": 1,
                    "dedupe_key": "regular-foreign-update",
                    "content": "must not update",
                    "memory_key": "must-not-update",
                    "memory_type": LongTermMemoryType.FACT.value,
                },
            )
        elif operation == "delete":
            response = await client.post(
                "/api/v1/memories/delete",
                json={"memory_id": foreign_id, "expected_version": 1, "dedupe_key": "regular-foreign-delete"},
            )
        else:
            response = await client.post(f"/api/v1/memories/{foreign_id}/resume-current", json={"expected_version": 1})
        _assert_standard(response, 404)
        _assert_standard(await client.get("/api/v1/memories/list", params={"uid": "user-b"}), 403)
        own_detail = _assert_standard(await client.get("/api/v1/memories/get", params={"memory_id": own_id}), 200)
        assert own_detail["data"]["owner_uid"] == "user-a"

    persisted_foreign = await memory_record_crud.get_by_id(db_session, uid="user-b", memory_id=foreign_id)
    assert persisted_foreign is not None
    assert (persisted_foreign.content, persisted_foreign.version, persisted_foreign.pinned, persisted_foreign.suppress_recall) == foreign_before
    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    assert await memory_job_crud.count(db_session, uid="user-b") == 0


@pytest.mark.asyncio
async def test_admin_can_create_foreign_jobs_idempotently_and_query_foreign_settings(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    channel = await _create_chat_channel(db_session, model_ids=[_chat_model()])
    assert channel.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        settings = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                params={"uid": "user-b"},
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel.id,
                    "organization_model_id": "organization-chat-model",
                },
            ),
            200,
        )
        assert settings["data"]["store"]["organization_channel_id"] == channel.id

        first = _assert_standard(
            await client.post(
                "/api/v1/memories/create",
                params={"uid": "user-b"},
                json=_memory_request(dedupe_key="admin-foreign-create", memory_key="foreign-created", content="foreign created"),
            ),
            200,
        )
        first_job_id = first["data"]["job"]["id"]
        assert first["data"]["status"] == "accepted"
        assert first["data"]["job"]["owner_uid"] == "user-b"
        duplicate = _assert_standard(
            await client.post(
                "/api/v1/memories/create",
                params={"uid": "user-b"},
                json=_memory_request(dedupe_key="admin-foreign-create", memory_key="foreign-created", content="foreign created"),
            ),
            200,
        )
        assert duplicate["data"]["status"] == "accepted"
        assert duplicate["data"]["job"]["id"] == first_job_id
        assert duplicate["data"]["job"]["owner_uid"] == "user-b"

        own = _assert_standard(
            await client.post(
                "/api/v1/memories/create",
                json=_memory_request(dedupe_key="admin-own-create", memory_key="own-created", content="own created"),
            ),
            200,
        )
        assert own["data"]["job"]["owner_uid"] == "user-a"

    foreign_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=first_job_id)
    own_job = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=own["data"]["job"]["id"])
    foreign_store = await memory_store_crud.get_by_uid(db_session, uid="user-b")
    own_store = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    assert foreign_job is not None and foreign_job.uid == "user-b"
    assert await memory_job_crud.count(db_session, uid="user-b") == 1
    assert own_job is not None and own_job.uid == "user-a"
    assert foreign_store is not None and foreign_store.organization_channel_id == channel.id
    assert own_store is not None and own_store.organization_channel_id is None


FOREIGN_SCOPE_CASES = (
    ("list", "GET", "/api/v1/memories/list", {"uid": "user-b"}, None),
    ("jobs", "GET", "/api/v1/memories/jobs", {"uid": "user-b"}, None),
    ("migrations", "GET", "/api/v1/memories/embedding-migrations", {"uid": "user-b"}, None),
    ("settings-get", "GET", "/api/v1/memories/settings", {"uid": "user-b"}, None),
    (
        "settings-post",
        "POST",
        "/api/v1/memories/settings",
        {"uid": "user-b"},
        {"auto_organize_enabled": False, "organization_channel_id": None, "organization_model_id": None},
    ),
    (
        "create",
        "POST",
        "/api/v1/memories/create",
        {"uid": "user-b"},
        _memory_request(dedupe_key="regular-foreign-create", memory_key="regular-foreign-create", content="blocked create"),
    ),
    ("organize", "POST", "/api/v1/memories/organize", {"uid": "user-b"}, {"dedupe_key": "regular-foreign-organize"}),
    ("reindex", "POST", "/api/v1/memories/reindex", {"uid": "user-b"}, {"dedupe_key": "regular-foreign-reindex"}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("_name,method,path,params,body", FOREIGN_SCOPE_CASES)
async def test_regular_user_cannot_select_foreign_scope_without_side_effects(
    _name: str,
    method: str,
    path: str,
    params: dict[str, str],
    body: dict | None,
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    before_b = await memory_job_crud.count(db_session, uid="user-b")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, params=params, json=body)
    _assert_standard(response, 403)
    assert await memory_job_crud.count(db_session, uid="user-b") == before_b == 0
    store_b = await memory_store_crud.get_by_uid(db_session, uid="user-b")
    assert store_b is not None and store_b.auto_organize_enabled is False


@pytest.mark.asyncio
async def test_admin_unknown_uid_is_not_created_or_scoped(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    current_user.is_superuser = True
    await _create_store(db_session, "user-a")

    requests = (
        ("GET", "/api/v1/memories/list", None),
        ("GET", "/api/v1/memories/jobs", None),
        ("GET", "/api/v1/memories/embedding-migrations", None),
        ("GET", "/api/v1/memories/settings", None),
        (
            "POST",
            "/api/v1/memories/settings",
            {"auto_organize_enabled": False, "organization_channel_id": None, "organization_model_id": None},
        ),
        ("POST", "/api/v1/memories/create", _memory_request(dedupe_key="unknown-create", memory_key="unknown", content="unknown")),
        ("POST", "/api/v1/memories/organize", {"dedupe_key": "unknown-organize"}),
        ("POST", "/api/v1/memories/reindex", {"dedupe_key": "unknown-reindex"}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, path, body in requests:
            response = await client.request(method, path, params={"uid": "user-missing"}, json=body)
            _assert_standard(response, 404)
    assert await memory_job_crud.count(db_session, uid="user-a") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/memories/create", {**_memory_request(dedupe_key="body-create", memory_key="body-create", content="body"), "uid": "user-b"}),
        (
            "/api/v1/memories/update",
            {
                "memory_id": 1,
                "expected_version": 1,
                "dedupe_key": "body-update",
                "content": "body",
                "memory_key": "body-update",
                "memory_type": LongTermMemoryType.FACT.value,
                "uid": "user-b",
            },
        ),
        ("/api/v1/memories/delete", {"memory_id": 1, "expected_version": 1, "dedupe_key": "body-delete", "uid": "user-b"}),
        (
            "/api/v1/memories/settings",
            {"auto_organize_enabled": False, "organization_channel_id": None, "organization_model_id": None, "uid": "user-b"},
        ),
        ("/api/v1/memories/organize", {"dedupe_key": "body-organize", "uid": "user-b"}),
        ("/api/v1/memories/reindex", {"dedupe_key": "body-reindex", "uid": "user-b"}),
    ],
)
async def test_memory_request_bodies_forbid_uid(path: str, body: dict, api_app: tuple[object, object], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)
    _assert_standard(response, 422)
    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    assert await memory_job_crud.count(db_session, uid="user-b") == 0


@pytest.mark.asyncio
async def test_admin_can_list_get_retry_cancel_foreign_jobs_and_regular_user_cannot(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    failed = await _create_job(
        db_session,
        uid="user-b",
        dedupe_key="foreign-create-failed",
        operation=LongTermMemoryMutationOperation.CREATE,
        status=LongTermMemoryMutationStatus.FAILED,
        payload=_publication_payload(key="retry-foreign", content="retry foreign"),
        error="create failed",
        finished_at=datetime.now(UTC),
    )
    assert failed.id is not None
    failed_job_id = int(failed.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        listed = _assert_standard(await client.get("/api/v1/memories/jobs"), 200)
        _assert_page(listed, total=1)
        assert listed["data"]["items"][0]["owner_uid"] == "user-b"
        detail = _assert_standard(await client.get(f"/api/v1/memories/jobs/{failed_job_id}"), 200)
        assert detail["data"]["owner_uid"] == "user-b"

        retried = _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_job_id}/retry"), 200)
        retry_job_id = retried["data"]["job"]["id"]
        assert retry_job_id != failed_job_id
        assert retried["data"]["job"]["owner_uid"] == "user-b"
        retry_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=retry_job_id)
        assert retry_job is not None and retry_job.uid == "user-b"

        cancelled = _assert_standard(await client.post(f"/api/v1/memories/jobs/{retry_job_id}/cancel"), 200)
        assert cancelled["data"]["accepted"] is True
        assert cancelled["data"]["job"]["owner_uid"] == "user-b"

        current_user.is_superuser = False
        _assert_standard(await client.get(f"/api/v1/memories/jobs/{failed_job_id}"), 404)
        _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_job_id}/retry"), 404)
        _assert_standard(await client.post(f"/api/v1/memories/jobs/{retry_job_id}/cancel"), 404)

    failed_after = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=failed_job_id)
    retry_after = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=retry_job_id)
    assert failed_after is not None and failed_after.uid == "user-b"
    assert retry_after is not None and retry_after.uid == "user-b" and retry_after.status == LongTermMemoryMutationStatus.CANCELLED
    assert await memory_job_crud.count(db_session, uid="user-a") == 0


@pytest.mark.asyncio
async def test_admin_migration_management_uses_each_owner_store_and_regular_user_isolated(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    payload_a = _migration_payload_for("user-a")
    payload_b = _migration_payload_for("user-b")
    job_a, revision_a = await _create_failed_migration(db_session, uid="user-a", payload=payload_a, cursor=2, total=3)
    job_b, revision_b = await _create_failed_migration(db_session, uid="user-b", payload=payload_b, cursor=7, total=9)
    assert job_a.id is not None and job_b.id is not None and revision_a.uid == "user-a" and revision_b.uid == "user-b"
    job_a_id = int(job_a.id)
    job_b_id = int(job_b.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        migrations = _assert_standard(await client.get("/api/v1/memories/embedding-migrations"), 200)
        _assert_page(migrations, total=2)
        by_owner = {item["owner_uid"]: item for item in migrations["data"]["items"]}
        assert by_owner["user-a"]["from"]["collection"] == "collection-user-a"
        assert by_owner["user-a"]["store"]["active_collection_name"] == "collection-user-a"
        assert by_owner["user-a"]["migration_cursor"] == 2
        assert by_owner["user-b"]["from"]["collection"] == "collection-user-b"
        assert by_owner["user-b"]["store"]["active_collection_name"] == "collection-user-b"
        assert by_owner["user-b"]["migration_cursor"] == 7

        detail = _assert_standard(await client.get(f"/api/v1/memories/embedding-migrations/{job_b_id}"), 200)["data"]
        assert detail["owner_uid"] == "user-b"
        assert detail["embedding_revision"]["owner_uid"] == "user-b"
        assert detail["store"]["active_collection_name"] == "collection-user-b"

        retried = _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{job_b_id}/retry"), 200)
        retry_job_id = retried["data"]["job"]["id"]
        assert retry_job_id != job_b_id
        retry_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=retry_job_id)
        retry_revision = await memory_embedding_revision_crud.get_by_job_id(db_session, uid="user-b", job_id=retry_job_id)
        store_a = await memory_store_crud.get_by_uid(db_session, uid="user-a")
        store_b = await memory_store_crud.get_by_uid(db_session, uid="user-b")
        assert retry_job is not None and retry_job.uid == "user-b" and retry_job.status == LongTermMemoryMutationStatus.PENDING
        assert retry_revision is not None and retry_revision.uid == "user-b"
        assert store_a is not None and store_a.migration_job_id == job_a_id
        assert store_b is not None and store_b.migration_job_id == retry_job_id

        cancelled = _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{retry_job_id}/cancel"), 200)
        assert cancelled["data"]["accepted"] is True
        cancelled_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=retry_job_id)
        assert cancelled_job is not None and cancelled_job.uid == "user-b"

        non_migration = await _create_job(
            db_session,
            uid="user-b",
            dedupe_key="foreign-non-migration",
            operation=LongTermMemoryMutationOperation.CREATE,
            status=LongTermMemoryMutationStatus.FAILED,
            payload=_publication_payload(key="not-migration", content="not migration"),
        )
        assert non_migration.id is not None
        non_migration_id = int(non_migration.id)
        _assert_standard(await client.get(f"/api/v1/memories/embedding-migrations/{non_migration_id}"), 404)

        current_user.is_superuser = False
        _assert_standard(await client.get(f"/api/v1/memories/embedding-migrations/{job_b_id}"), 404)
        _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{job_b_id}/retry"), 404)
        _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{job_b_id}/cancel"), 404)

    final_store_a = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    final_store_b = await memory_store_crud.get_by_uid(db_session, uid="user-b")
    assert final_store_a is not None and final_store_a.migration_job_id == job_a_id
    assert final_store_b is not None and final_store_b.uid == "user-b"


@pytest.mark.asyncio
async def test_admin_can_retry_foreign_collection_cleanup_but_regular_user_keeps_conflict(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    store_a = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    assert store_a is not None
    own_before = (store_a.old_collection_cleanup_status, store_a.old_collection_cleanup_job_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        submitted = _assert_standard(
            await client.post(
                "/api/v1/memories/reindex",
                params={"uid": "user-b"},
                json={"dedupe_key": "foreign-reindex-failed"},
            ),
            200,
        )
        failed_reindex_id = submitted["data"]["job"]["id"]
        failed_reindex = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=failed_reindex_id)
        store_b = await memory_store_crud.get_by_uid(db_session, uid="user-b")
        assert failed_reindex is not None and store_b is not None
        failed_reindex.status = LongTermMemoryMutationStatus.FAILED
        failed_reindex.active_mutation_key = None
        failed_reindex.error = "reindex failed"
        failed_reindex.finished_at = datetime.now(UTC)
        store_b.old_collection_cleanup_status = LongTermMemoryOldCollectionCleanupStatus.FAILED
        store_b.old_collection_name = "collection-user-b-old"
        store_b.old_collection_cleanup_job_id = failed_reindex.id
        store_b.index_status = LongTermMemoryIndexStatus.READY
        await db_session.commit()

        retried = _assert_standard(
            await client.post(
                f"/api/v1/memories/collections/{failed_reindex_id}/cleanup-retry",
                json={"dedupe_key": "foreign-cleanup-retry"},
            ),
            200,
        )
        retry_job_id = retried["data"]["job"]["id"]
        assert retried["data"]["created"] is True
        retry_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=retry_job_id)
        assert retry_job is not None
        assert retry_job.uid == "user-b"
        assert retry_job.operation == LongTermMemoryMutationOperation.REINDEX
        assert retry_job.parent_job_id == failed_reindex.id
        assert retry_job.status == LongTermMemoryMutationStatus.PENDING

        current_user.is_superuser = False
        before_regular_jobs = await memory_job_crud.count(db_session, uid="user-a")
        blocked = await client.post(
            f"/api/v1/memories/collections/{failed_reindex_id}/cleanup-retry",
            json={"dedupe_key": "regular-foreign-cleanup"},
        )
        _assert_standard(blocked, 409)
        assert await memory_job_crud.count(db_session, uid="user-a") == before_regular_jobs == 0
        assert (
            await memory_job_crud.get_by_dedupe_key(
                db_session,
                uid="user-a",
                dedupe_key="regular-foreign-cleanup",
            )
            is None
        )

    final_store_a = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    final_store_b = await memory_store_crud.get_by_uid(db_session, uid="user-b")
    failed_after = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=failed_reindex_id)
    assert final_store_a is not None and (final_store_a.old_collection_cleanup_status, final_store_a.old_collection_cleanup_job_id) == own_before
    assert final_store_b is not None and final_store_b.old_collection_cleanup_job_id == retry_job_id
    assert failed_after is not None and failed_after.uid == "user-b" and failed_after.status == LongTermMemoryMutationStatus.FAILED


@pytest.mark.asyncio
async def test_admin_can_read_foreign_history_after_record_physical_delete_but_get_cannot(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    record = await _create_record(db_session, uid="user-b", memory_key="deleted-record", content="deleted content")
    assert record.id is not None
    memory_id = int(record.id)
    await _create_history_revision(
        db_session,
        uid="user-b",
        memory_id=memory_id,
        memory_key="deleted-record",
        content="deleted history",
    )
    await db_session.delete(record)
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        history = _assert_standard(await client.get(f"/api/v1/memories/{memory_id}/history"), 200)
        _assert_page(history, total=1)
        history_item = history["data"]["items"][0]
        assert history_item["owner_uid"] == "user-b"
        assert history_item["content"] == "deleted history"
        _assert_standard(await client.get("/api/v1/memories/get", params={"memory_id": memory_id}), 404)

        current_user.is_superuser = False
        _assert_standard(await client.get(f"/api/v1/memories/{memory_id}/history"), 404)

    revision = await memory_revision_crud.get_by_memory_id(db_session, uid="user-b", memory_id=memory_id)
    assert revision is not None
    assert revision.uid == "user-b"
    assert revision.content == "deleted history"


@pytest.mark.asyncio
async def test_admin_can_organize_foreign_user_memories_without_cross_user_snapshot(
    api_app: tuple[object, object],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    channel = await _create_chat_channel(db_session, model_ids=[_chat_model()])
    assert channel.id is not None
    channel_id = int(channel.id)
    own = await _create_record(
        db_session,
        uid="user-a",
        memory_key="organize-own",
        content="own organization content",
        vector_item_id="organize-own-vector",
    )
    foreign = await _create_record(
        db_session,
        uid="user-b",
        memory_key="organize-foreign",
        content="foreign organization content",
        vector_item_id="organize-foreign-vector",
    )
    assert own.id is not None and foreign.id is not None
    own_id = int(own.id)
    foreign_id = int(foreign.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current_user.is_superuser = True
        _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                params={"uid": "user-b"},
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel_id,
                    "organization_model_id": "organization-chat-model",
                },
            ),
            200,
        )
        settings = _assert_standard(await client.get("/api/v1/memories/settings", params={"uid": "user-b"}), 200)
        assert settings["data"]["capacity"]["active_record_count"] == 1
        assert settings["data"]["store"]["active_record_count"] == 1

        organize = _assert_standard(
            await client.post(
                "/api/v1/memories/organize",
                params={"uid": "user-b"},
                json={"dedupe_key": "admin-foreign-organize"},
            ),
            200,
        )
        job_id = int(organize["data"]["job"]["id"])
        job_data = organize["data"]["job"]
        assert job_data["owner_uid"] == "user-b"
        assert job_data["payload"]["snapshot"]["count"] == 1
        assert [item["memory_id"] for item in job_data["payload"]["snapshot"]["items"]] == [foreign_id]

    persisted_job = await memory_job_crud.get_by_id(db_session, uid="user-b", job_id=job_id)
    assert persisted_job is not None and persisted_job.uid == "user-b"
    assert await memory_job_crud.count(db_session, uid="user-b") == 1
    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    assert own_id != foreign_id
