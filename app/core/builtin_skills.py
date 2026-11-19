import asyncio
import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import paths
from app.core.constants import (
    ERR_SKILL_MARKETPLACE_CONFLICT,
    ERR_SKILL_MARKETPLACE_INSTALL_FAILED,
    SKILL_MARKETPLACE_ID,
)
from app.core.crud.system.setting import system_setting_crud
from app.core.exceptions import ParameterException, ServerException


def skill_marketplace_installed(skills_root: Path) -> bool:
    marketplace_path = skills_root / SKILL_MARKETPLACE_ID
    try:
        marketplace_stat = marketplace_path.lstat()
    except FileNotFoundError:
        return False

    if not stat.S_ISDIR(marketplace_stat.st_mode) or getattr(marketplace_stat, "st_reparse_tag", 0):
        return False
    if not (marketplace_path / "SKILL.md").is_file():
        return False
    return (marketplace_path / "scripts" / "install_skill.py").is_file()


def _install_skill_marketplace(*, restore: bool) -> None:
    skills_root = paths.SKILLS_DIR
    builtin_skills_root = paths.BUILTIN_SKILLS_DIR
    target_path = skills_root / SKILL_MARKETPLACE_ID
    target_exists = os.path.lexists(target_path)

    if target_exists and not restore:
        return
    if target_exists and restore and skill_marketplace_installed(skills_root):
        return
    if target_exists and restore:
        raise ParameterException(ERR_SKILL_MARKETPLACE_CONFLICT)

    if not skill_marketplace_installed(builtin_skills_root):
        raise ServerException(ERR_SKILL_MARKETPLACE_INSTALL_FAILED)

    source_path = builtin_skills_root / SKILL_MARKETPLACE_ID
    skills_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".skill-marketplace-", dir=skills_root) as temporary_directory:
        staged_path = Path(temporary_directory) / SKILL_MARKETPLACE_ID
        shutil.copytree(source_path, staged_path)
        try:
            staged_path.rename(target_path)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if skill_marketplace_installed(skills_root):
                return
            raise ParameterException(ERR_SKILL_MARKETPLACE_CONFLICT) from exc


def restore_skill_marketplace() -> None:
    try:
        _install_skill_marketplace(restore=True)
    except (OSError, RuntimeError) as exc:
        raise ServerException(ERR_SKILL_MARKETPLACE_INSTALL_FAILED, cause=str(exc)) from exc


async def initialize_skill_marketplace(db: AsyncSession) -> None:
    try:
        first_initialization = await system_setting_crud.claim_skill_marketplace_initialization(db)
        if first_initialization:
            await asyncio.to_thread(_install_skill_marketplace, restore=False)
        await db.commit()
    except (OSError, RuntimeError) as exc:
        await db.rollback()
        raise ServerException(ERR_SKILL_MARKETPLACE_INSTALL_FAILED, cause=str(exc)) from exc
    except BaseException:
        await db.rollback()
        raise
