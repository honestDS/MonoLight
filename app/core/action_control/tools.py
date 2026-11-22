"""Map tool calls to system actions without coupling the action service to tools."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.action_control.contracts import PreparedAction
from app.core.action_control.service import (
    execute_action,
    is_action_available,
    prepare_action,
)
from app.core.constants import (
    ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED,
    ERR_SYSTEM_ACTION_UNKNOWN,
    SKILL_TOOL_NAME,
)
from app.core.crud.audit.audit import audit_crud
from app.core.exceptions import ForbiddenException, ParameterException
from app.models.audit import AuditDecision
from app.models.profile import ProfileConfig


@dataclass(frozen=True, slots=True)
class ToolActionBinding:
    """Describe one tool operation that maps to a system action."""

    tool_name: str
    operation: str
    action_id: str
    parameter_names: tuple[str, ...]
    description: str


def get_tool_action_bindings() -> tuple[ToolActionBinding, ...]:
    """Return the immutable tool-to-action mappings."""

    return (
        ToolActionBinding(
            tool_name=SKILL_TOOL_NAME,
            operation="uninstall",
            action_id="skills.uninstall",
            parameter_names=("skill_id",),
            description=("Operation uninstall permanently removes one installed shared skill package and all its resources for all users. Use it only for an explicit removal request. It requires an enabled super administrator and follows the active profile confirmation setting."),
        ),
    )


def get_tool_action_request(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Translate a recognized tool operation into an action request."""

    if not isinstance(arguments, dict):
        return None

    operation = arguments.get("operation")
    for binding in get_tool_action_bindings():
        if binding.tool_name != tool_name or binding.operation != operation:
            continue
        parameters: dict[str, Any] = {}
        for parameter_name in binding.parameter_names:
            if parameter_name not in arguments:
                continue
            parameters[parameter_name] = deepcopy(arguments[parameter_name])
        return binding.action_id, parameters
    return None


def restrict_tool_actions(tool_schemas: list[dict[str, Any]], actor: Any) -> list[dict[str, Any]]:
    """Restrict mapped operations in already-copied tool schemas."""

    for schema in tool_schemas:
        function = schema["function"]
        tool_name = function["name"]
        for binding in get_tool_action_bindings():
            if binding.tool_name != tool_name:
                continue
            operation = function["parameters"]["properties"]["operation"]
            if binding.operation not in operation["enum"]:
                continue
            if not is_action_available(binding.action_id, actor):
                operation["enum"] = [value for value in operation["enum"] if value != binding.operation]
                continue
            function["description"] = f"{function['description']} {binding.description}"
            operation["description"] = f"{operation['description']} {binding.description}"
    return tool_schemas


async def _load_bound_action(
    db: AsyncSession,
    round_record: Any,
    execution: Any,
    *,
    action_id: str,
    parameters: dict[str, Any],
    uid: str,
) -> tuple[PreparedAction, bool]:
    if round_record.uid != uid:
        raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED)
    details = await audit_crud.list_tool_details(db, round_record.id)
    detail = None
    for candidate in details:
        if candidate.id == execution.audit_tool_detail_id:
            detail = candidate
            break
    if detail is None or detail.decision_source != "system_rule" or not isinstance(detail.action_request, dict):
        raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED)
    try:
        prepared = PreparedAction.from_dict(detail.action_request)
    except ValueError as exc:
        raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED) from exc
    if prepared.action_id != action_id or prepared.parameters != parameters:
        raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED)
    return prepared, round_record.decision == AuditDecision.APPROVE


async def execute_tool_action(
    db: AsyncSession,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    uid: str,
    cfg: ProfileConfig,
    tool_call_id: str | None = None,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    """Execute a mapped system action using a validated confirmation record."""

    action_request = get_tool_action_request(tool_name, arguments)
    if action_request is None:
        raise ParameterException(ERR_SYSTEM_ACTION_UNKNOWN)
    action_id, parameters = action_request
    binding = None
    if tool_call_id is not None:
        binding = await audit_crud.get_running_execution_binding(db, new_tool_call_id=tool_call_id)
    if binding is None:
        prepared = await prepare_action(
            db,
            action_id=action_id,
            parameters=parameters,
            uid=uid,
            actor_uid=actor_uid,
        )
        approved = False
    else:
        round_record, execution = binding
        prepared, approved = await _load_bound_action(
            db,
            round_record,
            execution,
            action_id=action_id,
            parameters=parameters,
            uid=uid,
        )
    return await execute_action(
        db,
        prepared=prepared,
        uid=uid,
        cfg=cfg,
        approved=approved,
        actor_uid=actor_uid,
    )


__all__ = (
    "ToolActionBinding",
    "execute_tool_action",
    "get_tool_action_bindings",
    "get_tool_action_request",
    "restrict_tool_actions",
)
