import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.security as security
import app.core.skills as skills
from app.api.v1 import system
from app.core import paths
from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_SKILL_DELETE_FAILED,
    ERR_SKILL_ID_INVALID,
    ERR_SKILL_NOT_DIRECTORY,
    ERR_SKILL_NOT_FOUND,
    ERR_SKILL_PATH_INVALID,
    ERR_UNAUTHORIZED,
    MSG_SKILL_DELETED,
)
from app.core.i18n import t
from app.handler import register_handlers
from app.models.user import User

TEST_JWT_SECRET = "skills-management-integration-jwt-secret"
SKILLS_PATH = "/api/v1/system/skills"
DELETE_SKILL_PATH = f"{SKILLS_PATH}/delete"


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
    db_session.add_all(
        [
            User(uid="admin", username="admin", is_active=True, is_superuser=True, hashed_password="unused"),
            User(uid="ordinary", username="ordinary", is_active=True, is_superuser=False, hashed_password="unused"),
            User(uid="disabled", username="disabled", is_active=False, is_superuser=True, hashed_password="unused"),
        ]
    )
    await db_session.commit()

    @asynccontextmanager
    async def isolated_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(security, "AsyncSessionLocal", isolated_session)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(system.router, prefix="/api/v1")

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


def _assert_error(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == t(message)
    assert payload["data"] is None


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
    assert response.json()["data"] == {"items": [], "total": 0, "page": 1, "size": 10, "meta": None}
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
