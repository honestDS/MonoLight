import asyncio
import errno
import os
import stat
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.builtin_skills as builtin_skills
import app.core.security as security
import app.core.skills as skills
from app.api.v1 import system
from app.core import paths
from app.core.builtin_skills import initialize_skill_marketplace
from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_SKILL_DELETE_FAILED,
    ERR_SKILL_ID_INVALID,
    ERR_SKILL_MARKETPLACE_CONFLICT,
    ERR_SKILL_MARKETPLACE_INSTALL_FAILED,
    ERR_SKILL_NOT_DIRECTORY,
    ERR_SKILL_NOT_FOUND,
    ERR_SKILL_PATH_INVALID,
    ERR_UNAUTHORIZED,
    MSG_SKILL_DELETED,
    MSG_SKILL_MARKETPLACE_RESTORED,
    SKILL_MARKETPLACE_ID,
    SKILL_MARKETPLACE_INITIALIZED_KEY,
)
from app.core.i18n import t
from app.handler import register_handlers
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "skills-management-integration-jwt-secret"
SKILLS_PATH = "/api/v1/system/skills"
DELETE_SKILL_PATH = f"{SKILLS_PATH}/delete"
MARKETPLACE_RESTORE_PATH = f"{SKILLS_PATH}/marketplace/restore"


@pytest.fixture
def skills_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    monkeypatch.setattr(paths, "SKILLS_DIR", root)
    return root


@pytest_asyncio.fixture
async def api_client(
    db_session: AsyncSession,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    await db_session.run_sync(lambda sync_session: User.__table__.create(bind=sync_session.connection(), checkfirst=True))
    await db_session.run_sync(lambda sync_session: SystemSetting.__table__.create(bind=sync_session.connection(), checkfirst=True))
    db_session.add_all(
        [
            User(uid="admin", username="admin", is_active=True, is_superuser=True, hashed_password="unused"),
            User(uid="ordinary", username="ordinary", is_active=True, is_superuser=False, hashed_password="unused"),
            User(uid="disabled", username="disabled", is_active=False, is_superuser=True, hashed_password="unused"),
            SystemSetting(key=SKILL_MARKETPLACE_INITIALIZED_KEY, value="false"),
        ]
    )
    await db_session.commit()

    @asynccontextmanager
    async def isolated_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(security, "AsyncSessionLocal", isolated_session)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(system.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _request_skills(
    client: httpx.AsyncClient,
    method: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    url = SKILLS_PATH if method == "GET" else DELETE_SKILL_PATH
    return await client.request(method, url, headers=headers, params=params)


async def _restore_marketplace(client: httpx.AsyncClient, *, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.post(MARKETPLACE_RESTORE_PATH, headers=headers)


def _assert_error(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == t(message)
    assert payload["data"] is None


async def _marketplace_initialized_value(db_session: AsyncSession) -> str | None:
    result = await db_session.execute(select(SystemSetting.value).where(SystemSetting.key == SKILL_MARKETPLACE_INITIALIZED_KEY))
    return result.scalar_one_or_none()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...] | None:
    if not root.exists():
        return None

    entries: list[tuple[str, str, bytes | None]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as children:
            for child in sorted(children, key=lambda entry: entry.name):
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
                    entries.append((relative, "link", None))
                elif stat.S_ISDIR(info.st_mode):
                    entries.append((relative, "directory", None))
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    entries.append((relative, "file", path.read_bytes()))
                else:
                    entries.append((relative, "other", None))

    visit(root)
    return tuple(entries)


def _create_package(root: Path, skill_id: str, skill_markdown: str | None = None) -> Path:
    package = root / skill_id
    package.mkdir(parents=True, exist_ok=True)
    if skill_markdown is not None:
        (package / "SKILL.md").write_text(skill_markdown, encoding="utf-8")
    return package


def _admin_token() -> str:
    return _token("admin")


FORBIDDEN_REQUESTS = (
    {"id": "ordinary-get", "method": "GET", "claims": {}, "extra_params": {}},
    {"id": "ordinary-delete", "method": "POST", "claims": {}, "extra_params": {}},
    {
        "id": "forged-get",
        "method": "GET",
        "claims": {"is_superuser": True, "uid": "admin", "role": "admin"},
        "extra_params": {"uid": "admin", "role": "admin", "is_superuser": "true"},
    },
    {
        "id": "forged-delete",
        "method": "POST",
        "claims": {"is_superuser": True, "uid": "admin", "role": "admin"},
        "extra_params": {"uid": "admin", "role": "admin", "is_superuser": "true"},
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FORBIDDEN_REQUESTS, ids=lambda case: case["id"])
async def test_skills_api_rejects_ordinary_and_forged_admin_users(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    case: dict[str, Any],
) -> None:
    package = _create_package(skills_root, "owned", "---\nname: Owned\n---\n")
    (package / "private.txt").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)
    params = {"skill_id": "owned"} if case["method"] == "POST" else {}
    params.update(case["extra_params"])

    response = await _request_skills(
        api_client,
        case["method"],
        token=_token("ordinary", **case["claims"]),
        params=params,
    )

    _assert_error(response, 403, ERR_ONLY_ADMIN_ALLOWED)
    assert _tree_snapshot(skills_root) == before


UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", UNAUTHORIZED_CREDENTIALS)
@pytest.mark.parametrize("method", ("GET", "POST"))
async def test_skills_api_rejects_unauthorized_credentials_without_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    credential: str,
    method: str,
) -> None:
    package = _create_package(skills_root, "owned", "---\nname: Owned\n---\n")
    (package / "private.txt").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)

    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = security.create_access_token(data={"sub": "admin"}, expires_delta=timedelta(seconds=-1))
    else:
        token = _token("disabled")

    params = {"skill_id": "owned"} if method == "POST" else None
    response = await _request_skills(api_client, method, token=token, params=params)

    _assert_error(response, 401, ERR_UNAUTHORIZED)
    assert _tree_snapshot(skills_root) == before


@pytest.mark.asyncio
async def test_superuser_lists_skill_metadata_in_stable_pages_and_refreshes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    _create_package(skills_root, "alpha", "---\nname: Alpha Display\n---\n")
    _create_package(skills_root, "beta", "---\nname: Beta Display\ndescription: Beta description\n---\n")
    _create_package(skills_root, "delta")
    _create_package(skills_root, "zeta", "---\nname: Recovered Zeta\ndescription: [broken\n---\n")
    token = _admin_token()

    first_page = await _request_skills(api_client, "GET", token=token, params={"page": 1, "size": 2})
    assert first_page.status_code == 200
    first_data = first_page.json()["data"]
    assert first_data["total"] == 4
    assert first_data["page"] == 1
    assert first_data["size"] == 2
    assert [item["id"] for item in first_data["items"]] == ["alpha", "beta"]
    assert first_data["items"] == [
        {"id": "alpha", "name": "Alpha Display", "description": ""},
        {"id": "beta", "name": "Beta Display", "description": "Beta description"},
    ]
    assert all(set(item) == {"id", "name", "description"} for item in first_data["items"])

    second_page = await _request_skills(api_client, "GET", token=token, params={"page": 2, "size": 2})
    assert second_page.status_code == 200
    second_data = second_page.json()["data"]
    assert (second_data["total"], second_data["page"], second_data["size"]) == (4, 2, 2)
    assert second_data["items"] == [
        {"id": "delta", "name": "delta", "description": ""},
        {"id": "zeta", "name": "Recovered Zeta", "description": ""},
    ]

    (skills_root / "alpha" / "SKILL.md").write_text(
        "---\nname: Updated Alpha\ndescription: Updated description\n---\n",
        encoding="utf-8",
    )
    _create_package(skills_root, "epsilon", "---\nname: Epsilon\ndescription: New package\n---\n")
    refreshed = await _request_skills(api_client, "GET", token=token, params={"page": 1, "size": 10})
    assert refreshed.status_code == 200
    refreshed_data = refreshed.json()["data"]
    assert (refreshed_data["total"], refreshed_data["page"], refreshed_data["size"]) == (5, 1, 10)
    assert [item["id"] for item in refreshed_data["items"]] == ["alpha", "beta", "delta", "epsilon", "zeta"]
    assert refreshed_data["items"][0] == {
        "id": "alpha",
        "name": "Updated Alpha",
        "description": "Updated description",
    }


@pytest.mark.asyncio
async def test_listing_missing_skills_root_returns_empty_without_creating_it(
    api_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing-skills"
    monkeypatch.setattr(paths, "SKILLS_DIR", missing_root)

    response = await _request_skills(api_client, "GET", token=_admin_token())

    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "total": 0, "page": 1, "size": 10, "meta": {"marketplace_installed": False}}
    assert not missing_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("params", ({"page": 0}, {"size": 201}), ids=("page-zero", "size-over-limit"))
async def test_listing_rejects_invalid_pagination(
    api_client: httpx.AsyncClient,
    params: dict[str, int],
) -> None:
    response = await _request_skills(api_client, "GET", token=_admin_token(), params=params)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_superuser_deletes_only_the_requested_package_and_cannot_repeat_delete(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    tmp_path: Path,
) -> None:
    package = _create_package(skills_root, "remove-me", "---\nname: Remove Me\n---\n")
    nested = package / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("delete", encoding="utf-8")
    sibling = _create_package(skills_root, "keep-me", "---\nname: Keep Me\n---\n")
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    outside = tmp_path / "outside-sentinel.txt"
    outside.write_text("untouched", encoding="utf-8")
    token = _admin_token()

    deleted = await _request_skills(api_client, "POST", token=token, params={"skill_id": "remove-me"})
    assert deleted.status_code == 200
    assert deleted.json()["message"] == t(MSG_SKILL_DELETED)
    assert not package.exists()
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert outside.read_text(encoding="utf-8") == "untouched"

    listed = await _request_skills(api_client, "GET", token=token)
    assert [item["id"] for item in listed.json()["data"]["items"]] == ["keep-me"]

    repeated = await _request_skills(api_client, "POST", token=token, params={"skill_id": "remove-me"})
    _assert_error(repeated, 404, ERR_SKILL_NOT_FOUND)
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_superuser_can_delete_package_without_skill_markdown(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    package = _create_package(skills_root, "no-document")
    (package / "payload.txt").write_text("remove", encoding="utf-8")

    response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "no-document"})

    assert response.status_code == 200
    assert response.json()["message"] == t(MSG_SKILL_DELETED)
    assert not package.exists()


@pytest.mark.asyncio
async def test_delete_rejects_invalid_ids_without_filesystem_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    tmp_path: Path,
) -> None:
    package = _create_package(skills_root, "preserved")
    (package / "payload.txt").write_text("preserve", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve", encoding="utf-8")
    invalid_ids = (
        "..",
        "../outside",
        str((tmp_path / "absolute-target").resolve()),
        r"C:\outside",
        r"..\outside",
        ".hidden",
    )
    before = _tree_snapshot(skills_root)

    for skill_id in invalid_ids:
        response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": skill_id})
        _assert_error(response, 400, ERR_SKILL_ID_INVALID)
        assert _tree_snapshot(skills_root) == before
        assert outside.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_delete_reports_missing_and_non_directory_targets_without_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    skills_root.mkdir()
    (skills_root / "plain-file").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)

    non_directory = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "plain-file"})
    _assert_error(non_directory, 400, ERR_SKILL_NOT_DIRECTORY)

    missing = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "missing"})
    _assert_error(missing, 404, ERR_SKILL_NOT_FOUND)
    assert _tree_snapshot(skills_root) == before


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")


@pytest.mark.asyncio
async def test_deleting_package_does_not_follow_external_directory_symlink(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    tmp_path: Path,
) -> None:
    package = _create_package(skills_root, "linked-package")
    external = tmp_path / "external-directory"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    _symlink_or_skip(external, package / "external-link", target_is_directory=True)

    response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "linked-package"})

    assert response.status_code == 200
    assert not package.exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_deleting_top_level_internal_symlink_removes_only_the_alias(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    target = _create_package(skills_root, "real-package")
    target_file = target / "target.txt"
    target_file.write_text("preserve", encoding="utf-8")
    alias = skills_root / "package-alias"
    _symlink_or_skip(target, alias, target_is_directory=True)

    response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "package-alias"})

    assert response.status_code == 200
    assert not alias.is_symlink()
    assert target_file.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_delete_rejects_external_and_root_symlink_aliases(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    tmp_path: Path,
) -> None:
    skills_root.mkdir()
    external = tmp_path / "external-package"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    external_alias = skills_root / "external-alias"
    root_alias = skills_root / "root-alias"
    _symlink_or_skip(external, external_alias, target_is_directory=True)
    _symlink_or_skip(skills_root, root_alias, target_is_directory=True)
    before = _tree_snapshot(skills_root)

    for skill_id in ("external-alias", "root-alias"):
        response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": skill_id})
        _assert_error(response, 400, ERR_SKILL_PATH_INVALID)
        assert _tree_snapshot(skills_root) == before
        assert sentinel.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_filesystem_delete_failure_returns_error_without_claiming_success(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _create_package(skills_root, "blocked", "---\nname: Blocked\n---\n")
    (package / "payload.txt").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)

    def fail_rmtree(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("simulated filesystem permission failure")

    monkeypatch.setattr(skills.shutil, "rmtree", fail_rmtree)
    response = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": "blocked"})

    _assert_error(response, 500, ERR_SKILL_DELETE_FAILED)
    assert _tree_snapshot(skills_root) == before


@pytest.mark.asyncio
async def test_admin_can_restore_the_complete_skill_marketplace_package(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    source = paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID
    source_snapshot = _tree_snapshot(source)
    assert source_snapshot is not None

    response = await _restore_marketplace(api_client, token=_admin_token())

    assert response.status_code == 200
    assert response.json()["message"] == t(MSG_SKILL_MARKETPLACE_RESTORED)
    assert response.json()["data"] is None
    assert _tree_snapshot(skills_root / SKILL_MARKETPLACE_ID) == source_snapshot


@pytest.mark.asyncio
async def test_marketplace_status_pagination_and_repeat_restore_preserve_user_content(
    api_client: httpx.AsyncClient,
    skills_root: Path,
) -> None:
    _create_package(skills_root, "alpha", "---\nname: Alpha\n---\n")
    token = _admin_token()

    before_restore = await _request_skills(api_client, "GET", token=token, params={"page": 1, "size": 1})
    assert before_restore.status_code == 200
    before_data = before_restore.json()["data"]
    assert before_data["total"] == 1
    assert before_data["items"] == [{"id": "alpha", "name": "Alpha", "description": ""}]
    assert before_data["meta"] == {"marketplace_installed": False}

    empty_page = await _request_skills(api_client, "GET", token=token, params={"page": 2, "size": 1})
    assert empty_page.status_code == 200
    assert empty_page.json()["data"] == {
        "items": [],
        "total": 1,
        "page": 2,
        "size": 1,
        "meta": {"marketplace_installed": False},
    }
    assert not (skills_root / SKILL_MARKETPLACE_ID).exists()

    restored = await _restore_marketplace(api_client, token=token)
    assert restored.status_code == 200
    marketplace = skills_root / SKILL_MARKETPLACE_ID
    (marketplace / "SKILL.md").write_text("custom marketplace contents\n", encoding="utf-8")
    (marketplace / "custom.txt").write_text("keep this file\n", encoding="utf-8")
    other = _create_package(skills_root, "zz-other", "---\nname: Other\n---\n")
    (other / "keep.txt").write_text("keep other skill\n", encoding="utf-8")
    marketplace_before_repeat = _tree_snapshot(marketplace)

    for page in (1, 2, 3):
        listed = await _request_skills(api_client, "GET", token=token, params={"page": page, "size": 1})
        assert listed.status_code == 200
        listed_data = listed.json()["data"]
        assert listed_data["total"] == 3
        assert listed_data["meta"] == {"marketplace_installed": True}

    repeated = await _restore_marketplace(api_client, token=token)

    assert repeated.status_code == 200
    assert repeated.json()["message"] == t(MSG_SKILL_MARKETPLACE_RESTORED)
    assert _tree_snapshot(marketplace) == marketplace_before_repeat
    assert (skills_root / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "---\nname: Alpha\n---\n"
    assert (other / "keep.txt").read_text(encoding="utf-8") == "keep other skill\n"


@pytest.mark.asyncio
async def test_initialize_then_http_restore_lifecycle_does_not_reinstall_after_claim(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    skills_root: Path,
) -> None:
    source_snapshot = _tree_snapshot(paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID)
    assert source_snapshot is not None
    marketplace = skills_root / SKILL_MARKETPLACE_ID

    await initialize_skill_marketplace(db_session)
    assert _tree_snapshot(marketplace) == source_snapshot
    assert await _marketplace_initialized_value(db_session) == "true"

    deleted = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted.status_code == 200
    assert not marketplace.exists()

    await initialize_skill_marketplace(db_session)
    assert not marketplace.exists()

    restored = await _restore_marketplace(api_client, token=_admin_token())
    assert restored.status_code == 200
    assert _tree_snapshot(marketplace) == source_snapshot

    deleted_again = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted_again.status_code == 200
    assert not marketplace.exists()

    await initialize_skill_marketplace(db_session)
    assert not marketplace.exists()


RESTORE_UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", RESTORE_UNAUTHORIZED_CREDENTIALS)
async def test_marketplace_restore_rejects_unauthorized_credentials_without_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    credential: str,
) -> None:
    package = _create_package(skills_root, "owned", "---\nname: Owned\n---\n")
    (package / "private.txt").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)

    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = security.create_access_token(data={"sub": "admin"}, expires_delta=timedelta(seconds=-1))
    else:
        token = _token("disabled")

    response = await _restore_marketplace(api_client, token=token)

    _assert_error(response, 401, ERR_UNAUTHORIZED)
    assert _tree_snapshot(skills_root) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    ({}, {"is_superuser": True, "uid": "admin", "role": "admin"}),
    ids=("ordinary", "forged-admin"),
)
async def test_marketplace_restore_rejects_ordinary_and_forged_admin_users_without_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    claims: dict[str, Any],
) -> None:
    package = _create_package(skills_root, "owned", "---\nname: Owned\n---\n")
    (package / "private.txt").write_text("preserve", encoding="utf-8")
    before = _tree_snapshot(skills_root)

    response = await _restore_marketplace(api_client, token=_token("ordinary", **claims))

    _assert_error(response, 403, ERR_ONLY_ADMIN_ALLOWED)
    assert _tree_snapshot(skills_root) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_type",
    ("file", "incomplete-directory", "link"),
    ids=("file", "incomplete-directory", "link"),
)
async def test_marketplace_restore_rejects_conflicting_targets_without_changes(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    target_type: str,
) -> None:
    skills_root.mkdir()
    target = skills_root / SKILL_MARKETPLACE_ID
    if target_type == "file":
        target.write_text("custom file", encoding="utf-8")
    elif target_type == "incomplete-directory":
        target.mkdir()
        (target / "SKILL.md").write_text("custom incomplete package", encoding="utf-8")
        (target / "custom.txt").write_text("preserve", encoding="utf-8")
    if target_type == "link":
        external = skills_root.parent / "external-marketplace"
        external.mkdir()
        (external / "sentinel.txt").write_text("preserve", encoding="utf-8")
        _symlink_or_skip(external, target, target_is_directory=True)
    before = _tree_snapshot(skills_root)

    response = await _restore_marketplace(api_client, token=_admin_token())

    _assert_error(response, 400, ERR_SKILL_MARKETPLACE_CONFLICT)
    assert _tree_snapshot(skills_root) == before
    if target_type == "link":
        assert (target / "sentinel.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_marketplace_restore_missing_resource_is_retryable_without_partial_files(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    skills_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_root = paths.BUILTIN_SKILLS_DIR
    source_snapshot = _tree_snapshot(builtin_root / SKILL_MARKETPLACE_ID)
    assert source_snapshot is not None
    missing_root = tmp_path / "missing-builtin-skills"
    monkeypatch.setattr(paths, "BUILTIN_SKILLS_DIR", missing_root)

    failed = await _restore_marketplace(api_client, token=_admin_token())

    _assert_error(failed, 500, ERR_SKILL_MARKETPLACE_INSTALL_FAILED)
    assert await _marketplace_initialized_value(db_session) == "false"
    assert _tree_snapshot(skills_root) in (None, ())
    assert not (skills_root / SKILL_MARKETPLACE_ID).exists()

    monkeypatch.setattr(paths, "BUILTIN_SKILLS_DIR", builtin_root)
    retried = await _restore_marketplace(api_client, token=_admin_token())

    assert retried.status_code == 200
    assert _tree_snapshot(skills_root / SKILL_MARKETPLACE_ID) == source_snapshot


@pytest.mark.asyncio
async def test_marketplace_restore_copy_permission_error_is_retryable_without_partial_files(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_snapshot = _tree_snapshot(paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID)
    assert source_snapshot is not None

    def fail_copytree(_source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        staged_path = Path(destination)
        staged_path.mkdir(parents=True)
        (staged_path / "partial.txt").write_text("partial copy", encoding="utf-8")
        raise PermissionError("simulated marketplace copy permission failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(builtin_skills.shutil, "copytree", fail_copytree)
        failed = await _restore_marketplace(api_client, token=_admin_token())

    _assert_error(failed, 500, ERR_SKILL_MARKETPLACE_INSTALL_FAILED)
    assert await _marketplace_initialized_value(db_session) == "false"
    assert _tree_snapshot(skills_root) in (None, ())
    assert not (skills_root / SKILL_MARKETPLACE_ID).exists()

    retried = await _restore_marketplace(api_client, token=_admin_token())

    assert retried.status_code == 200
    assert _tree_snapshot(skills_root / SKILL_MARKETPLACE_ID) == source_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("operational-error", "runtime-error"), ids=("operational-error", "runtime-error"))
async def test_marketplace_restore_and_listing_do_not_require_database_commit(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source_snapshot = _tree_snapshot(paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID)
    assert source_snapshot is not None
    marketplace = skills_root / SKILL_MARKETPLACE_ID

    await initialize_skill_marketplace(db_session)
    assert _tree_snapshot(marketplace) == source_snapshot

    deleted = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted.status_code == 200
    assert not marketplace.exists()
    assert await _marketplace_initialized_value(db_session) == "true"

    async def fail_commit() -> None:
        if failure == "operational-error":
            raise OperationalError("COMMIT", {}, RuntimeError("simulated commit failure"))
        raise RuntimeError("simulated commit failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(db_session, "commit", fail_commit)

        restored = await _restore_marketplace(api_client, token=_admin_token())
        assert restored.status_code == 200
        assert restored.json()["message"] == t(MSG_SKILL_MARKETPLACE_RESTORED)
        assert _tree_snapshot(marketplace) == source_snapshot

        listed = await _request_skills(api_client, "GET", token=_admin_token())
        assert listed.status_code == 200
        assert listed.json()["data"]["meta"]["marketplace_installed"] is True
        assert await _marketplace_initialized_value(db_session) == "true"

    deleted_again = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted_again.status_code == 200
    assert not marketplace.exists()

    await initialize_skill_marketplace(db_session)
    assert not marketplace.exists()


@pytest.mark.asyncio
async def test_concurrent_marketplace_restore_publishes_one_complete_package(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID
    source_snapshot = _tree_snapshot(source)
    assert source_snapshot is not None
    marketplace = skills_root / SKILL_MARKETPLACE_ID

    await initialize_skill_marketplace(db_session)
    assert _tree_snapshot(marketplace) == source_snapshot

    deleted = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted.status_code == 200
    assert not marketplace.exists()
    assert await _marketplace_initialized_value(db_session) == "true"

    original_copytree = builtin_skills.shutil.copytree
    source_path = paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID
    barrier = threading.Barrier(8, timeout=15)

    def synchronized_copytree(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        copied = original_copytree(source, destination, *args, **kwargs)
        if Path(source) == source_path:
            barrier.wait()
        return copied

    with monkeypatch.context() as concurrency_patch:
        concurrency_patch.setattr(builtin_skills.shutil, "copytree", synchronized_copytree)
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [loop.run_in_executor(executor, builtin_skills.restore_skill_marketplace) for _ in range(8)]
            results = await asyncio.gather(*futures, return_exceptions=True)

    assert results == [None] * 8
    assert _tree_snapshot(marketplace) == source_snapshot
    marketplace_entries = [entry for entry in skills_root.iterdir() if entry.name == SKILL_MARKETPLACE_ID]
    assert len(marketplace_entries) == 1
    assert not any(entry.name.startswith(".skill-marketplace-") for entry in skills_root.iterdir())

    listed = await _request_skills(api_client, "GET", token=_admin_token())
    assert listed.status_code == 200
    assert listed.json()["data"]["meta"]["marketplace_installed"] is True
    assert await _marketplace_initialized_value(db_session) == "true"

    deleted_again = await _request_skills(api_client, "POST", token=_admin_token(), params={"skill_id": SKILL_MARKETPLACE_ID})
    assert deleted_again.status_code == 200
    assert not marketplace.exists()

    await initialize_skill_marketplace(db_session)
    assert not marketplace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        {"id": "eexist-complete", "error": errno.EEXIST, "target": "complete", "status": 200},
        {"id": "enotempty-complete", "error": errno.ENOTEMPTY, "target": "complete", "status": 200},
        {"id": "eexist-file", "error": errno.EEXIST, "target": "file", "status": 400},
        {"id": "eacces-missing", "error": errno.EACCES, "target": "missing", "status": 500},
    ),
    ids=lambda case: case["id"],
)
async def test_marketplace_restore_classifies_publish_race_errors(
    api_client: httpx.AsyncClient,
    skills_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
) -> None:
    source_snapshot = _tree_snapshot(paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID)
    assert source_snapshot is not None
    marketplace = skills_root / SKILL_MARKETPLACE_ID
    custom_contents = f"published by another request: {case['id']}\n"

    def fake_rename(staged_path: Path, target_path: Path) -> Path:
        if case["target"] == "complete":
            builtin_skills.shutil.copytree(staged_path, target_path)
            (target_path / "custom.txt").write_text(custom_contents, encoding="utf-8")
        elif case["target"] == "file":
            target_path.write_text("existing custom marketplace file\n", encoding="utf-8")
        raise OSError(case["error"], f"simulated publish error: {case['id']}")

    with monkeypatch.context() as rename_patch:
        rename_patch.setattr(Path, "rename", fake_rename)
        response = await _restore_marketplace(api_client, token=_admin_token())

    if case["status"] == 200:
        assert response.status_code == 200
        assert response.json()["message"] == t(MSG_SKILL_MARKETPLACE_RESTORED)
        actual_snapshot = _tree_snapshot(marketplace)
        assert actual_snapshot is not None
        assert all(entry in actual_snapshot for entry in source_snapshot)
        assert (marketplace / "custom.txt").read_text(encoding="utf-8") == custom_contents

        listed = await _request_skills(api_client, "GET", token=_admin_token())
        assert listed.status_code == 200
        assert listed.json()["data"]["meta"]["marketplace_installed"] is True
    elif case["target"] == "file":
        _assert_error(response, 400, ERR_SKILL_MARKETPLACE_CONFLICT)
        assert marketplace.read_text(encoding="utf-8") == "existing custom marketplace file\n"
    else:
        _assert_error(response, 500, ERR_SKILL_MARKETPLACE_INSTALL_FAILED)
        assert not marketplace.exists()

    assert not any(entry.name.startswith(".skill-marketplace-") for entry in skills_root.iterdir())
