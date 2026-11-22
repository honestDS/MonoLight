"""技能相关系统操作。"""

from __future__ import annotations

from typing import Any

from app.core import paths
from app.core.constants import ERR_SYSTEM_ACTION_UNKNOWN, MSG_SKILL_DELETED, MSG_SKILL_UNINSTALL_CONFIRMATION
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.skills import delete_skill, prepare_skill_deletion

from .contracts import ActionDefinition


def _prepare_uninstall(parameters: dict[str, Any]) -> dict[str, Any]:
    if set(parameters) - {"skill_id"}:
        raise ParameterException(ERR_SYSTEM_ACTION_UNKNOWN)
    return prepare_skill_deletion(paths.SKILLS_DIR, parameters.get("skill_id"))


def _execute_uninstall(parameters: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    delete_skill(paths.SKILLS_DIR, parameters["skill_id"], expected_target=target)
    return {
        "status": "success",
        "operation": "uninstall",
        "skill_id": parameters["skill_id"],
        "message": t(MSG_SKILL_DELETED),
    }


def get_skill_uninstall_definition() -> ActionDefinition:
    return ActionDefinition(
        action_id="skills.uninstall",
        permission="superuser",
        prepare=_prepare_uninstall,
        execute=_execute_uninstall,
        description_key=MSG_SKILL_UNINSTALL_CONFIRMATION,
    )
