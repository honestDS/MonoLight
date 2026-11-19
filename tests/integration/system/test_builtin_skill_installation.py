from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.providers.database.bootstrap as bootstrap
from app.core import builtin_skills, paths
from app.core.constants import SKILL_MARKETPLACE_ID, SKILL_MARKETPLACE_INITIALIZED_KEY
from app.core.crud.system.setting import system_setting_crud
from app.core.exceptions import ServerException
from app.core.skills import delete_skill, list_skills
from app.models.user import User


@pytest_asyncio.fixture
async def isolated_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_factory):
    migration_scripts_dir = tmp_path / "migrations"
    migration_scripts_dir.mkdir()
    skills_dir = tmp_path / "data" / "skills"
    monkeypatch.setattr(paths, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(bootstrap, "MIGRATION_SCRIPTS_DIR", migration_scripts_dir)

    async with database_factory(create_schema=False, name="marketplace.sqlite3", wal=True) as factory:
        monkeypatch.setattr(bootstrap, "engine", factory.kw["bind"])
        yield factory, skills_dir


def _package_bytes(package: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        files[path.relative_to(package).as_posix()] = path.read_bytes()
    return files


def _assert_installed_marketplace(skills_dir: Path) -> Path:
    source_path = paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID
    target_path = skills_dir / SKILL_MARKETPLACE_ID

    assert target_path.is_dir()
    assert _package_bytes(target_path) == _package_bytes(source_path)

    listing = list_skills(skills_dir)
    assert listing["status"] == "success"
    assert listing["total"] == 1
    assert len(listing["skills"]) == 1
    marketplace = listing["skills"][0]
    assert marketplace["id"] == SKILL_MARKETPLACE_ID
    assert marketplace["metadata_status"] == "valid"
    assert marketplace["document_path"] == str((target_path / "SKILL.md").resolve())
    return target_path


async def _run_list_installed(skills_dir: Path, tmp_path: Path) -> dict[str, Any]:
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    environment = os.environ.copy()
    for variable in ("MONOLIGHT_SKILLS_DIR", "AGENT_SKILLS_DIR", "SKILLS_DIR"):
        environment.pop(variable, None)

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(skills_dir / SKILL_MARKETPLACE_ID / "scripts" / "install_skill.py"),
        "list-installed",
        cwd=str(unrelated_cwd),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")

    result = json.loads(stdout.decode("utf-8"))
    assert result["status"] == "success"
    assert result["command"] == "list-installed"
    assert result["skills_dir"] == str(skills_dir.resolve())
    assert result["resolved_by"].startswith("self:")
    assert result["count"] == 1
    assert len(result["skills"]) == 1
    installed = result["skills"][0]
    assert installed["id"] == SKILL_MARKETPLACE_ID
    assert installed["metadata_valid"] is True
    return result


async def _initialize_marketplace_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
    count: int = 8,
) -> None:
    async def initialize_once() -> None:
        async with session_factory() as session:
            await builtin_skills.initialize_skill_marketplace(session)

    await asyncio.gather(*(initialize_once() for _ in range(count)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment",
    [
        pytest.param("fresh", id="fresh"),
        pytest.param("legacy_admin", id="legacy_admin"),
    ],
)
async def test_first_bootstrap_installs_builtin_marketplace(
    deployment: str,
    isolated_marketplace,
    tmp_path: Path,
) -> None:
    session_factory, skills_dir = isolated_marketplace

    if deployment == "legacy_admin":
        await bootstrap.create_database_tables()
        async with session_factory() as session:
            session.add(User(uid="legacy-admin", username="legacy-admin", is_superuser=True))
            await session.commit()

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    _assert_installed_marketplace(skills_dir)
    await _run_list_installed(skills_dir, tmp_path)


@pytest.mark.asyncio
async def test_deleted_marketplace_is_not_reinstalled_when_published_source_is_missing(
    isolated_marketplace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, skills_dir = isolated_marketplace

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    target_path = _assert_installed_marketplace(skills_dir)
    delete_skill(skills_dir, SKILL_MARKETPLACE_ID)
    assert not target_path.exists()

    missing_published_skills = tmp_path / "missing-published-skills"
    monkeypatch.setattr(paths, "BUILTIN_SKILLS_DIR", missing_published_skills)
    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    assert not target_path.exists()
    assert list_skills(skills_dir)["skills"] == []


@pytest.mark.asyncio
async def test_first_bootstrap_preserves_existing_custom_marketplace_and_later_deletion(
    isolated_marketplace,
) -> None:
    session_factory, skills_dir = isolated_marketplace
    target_path = skills_dir / SKILL_MARKETPLACE_ID
    custom_files = {
        "custom.txt": b"custom marketplace content",
        "private/config.bin": b"private bytes",
    }
    for relative_path, content in custom_files.items():
        custom_file = target_path / relative_path
        custom_file.parent.mkdir(parents=True, exist_ok=True)
        custom_file.write_bytes(content)
    original_bytes = _package_bytes(target_path)

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    assert _package_bytes(target_path) == original_bytes
    listing = list_skills(skills_dir)
    assert listing["total"] == 1
    assert listing["skills"][0]["id"] == SKILL_MARKETPLACE_ID
    assert listing["skills"][0]["metadata_status"] == "missing"

    delete_skill(skills_dir, SKILL_MARKETPLACE_ID)
    assert not target_path.exists()

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    assert not target_path.exists()
    assert list_skills(skills_dir)["skills"] == []


@pytest.mark.asyncio
async def test_failed_staged_copy_rolls_back_initialization_and_can_retry(
    isolated_marketplace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, skills_dir = isolated_marketplace
    original_copytree = builtin_skills.shutil.copytree

    def fail_after_partial_copy(
        _source: Path,
        destination: Path,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        destination.mkdir()
        (destination / "partial.txt").write_bytes(b"partial")
        raise PermissionError("simulated staged copy failure")

    monkeypatch.setattr(builtin_skills.shutil, "copytree", fail_after_partial_copy)
    with pytest.raises(ServerException):
        async with session_factory() as session:
            await bootstrap.init_database_schema(session)

    monkeypatch.setattr(builtin_skills.shutil, "copytree", original_copytree)
    target_path = skills_dir / SKILL_MARKETPLACE_ID
    assert not target_path.exists()
    assert list(skills_dir.glob(".skill-marketplace-*")) == []

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    _assert_installed_marketplace(skills_dir)
    assert list(skills_dir.glob(".skill-marketplace-*")) == []


@pytest.mark.asyncio
async def test_concurrent_file_sqlite_initialization_is_persistent_after_deletion(
    isolated_marketplace,
) -> None:
    session_factory, skills_dir = isolated_marketplace

    await bootstrap.create_database_tables()
    await _initialize_marketplace_concurrently(session_factory)
    target_path = _assert_installed_marketplace(skills_dir)

    delete_skill(skills_dir, SKILL_MARKETPLACE_ID)
    assert not target_path.exists()

    await _initialize_marketplace_concurrently(session_factory)

    assert not target_path.exists()
    assert list_skills(skills_dir)["skills"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_failure",
    ("not-committed", "committed-response-lost"),
    ids=("not-committed", "committed-response-lost"),
)
async def test_bootstrap_recovers_after_marketplace_commit_result_is_lost(
    isolated_marketplace,
    monkeypatch: pytest.MonkeyPatch,
    commit_failure: str,
) -> None:
    session_factory, skills_dir = isolated_marketplace
    source_path = paths.BUILTIN_SKILLS_DIR / SKILL_MARKETPLACE_ID
    source_bytes = _package_bytes(source_path)
    target_path = skills_dir / SKILL_MARKETPLACE_ID

    with monkeypatch.context() as failure_patch:
        async with session_factory() as session:
            original_commit = session.commit

            async def commit_with_unknown_result() -> None:
                if not target_path.is_dir() or _package_bytes(target_path) != source_bytes:
                    await original_commit()
                    return
                if commit_failure == "committed-response-lost":
                    await original_commit()
                raise OperationalError("COMMIT", {}, RuntimeError("simulated commit result loss"))

            failure_patch.setattr(session, "commit", commit_with_unknown_result)
            with pytest.raises(OperationalError):
                await bootstrap.init_database_schema(session)

    assert _package_bytes(target_path) == source_bytes
    custom_path = target_path / "custom.txt"
    custom_content = b"preserve this custom file"
    custom_path.write_bytes(custom_content)

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)
        setting = await system_setting_crud.get_by_key(session, key=SKILL_MARKETPLACE_INITIALIZED_KEY)
        assert setting is not None
        assert setting.value == "true"

    for relative_path, content in source_bytes.items():
        assert (target_path / relative_path).read_bytes() == content
    assert custom_path.read_bytes() == custom_content

    delete_skill(skills_dir, SKILL_MARKETPLACE_ID)
    assert not target_path.exists()

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

    assert not target_path.exists()
    assert list_skills(skills_dir)["skills"] == []
