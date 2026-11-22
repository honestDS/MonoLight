"""Generic control service for business system actions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED,
    ERR_SYSTEM_ACTION_PERMISSION_DENIED,
    ERR_SYSTEM_ACTION_TARGET_CHANGED,
    ERR_SYSTEM_ACTION_UNKNOWN,
)
from app.core.crud.account.user import user_crud
from app.core.exceptions import ForbiddenException, ParameterException
from app.core.i18n import t
from app.models.profile import ProfileConfig
from app.models.user import User

from .actor import resolve_action_actor
from .contracts import ActionDefinition, PreparedAction
from .skills import get_skill_uninstall_definition

__all__ = (
    "get_action_definition",
    "is_action_available",
    "prepare_action",
    "validate_prepared_action",
    "describe_action",
    "requires_action_confirmation",
    "execute_action",
)


def get_action_definition(action_id: str) -> ActionDefinition:
    """Return the static definition for a system action."""

    for definition in (get_skill_uninstall_definition(),):
        if definition.action_id == action_id:
            return definition
    raise ParameterException(ERR_SYSTEM_ACTION_UNKNOWN)


def _has_action_permission(definition: ActionDefinition, actor: User | None) -> bool:
    return bool(actor is not None and definition.permission == "superuser" and actor.is_active and actor.is_superuser)


def _require_action_permission(definition: ActionDefinition, actor: User | None) -> User:
    if not _has_action_permission(definition, actor):
        raise ForbiddenException(ERR_SYSTEM_ACTION_PERMISSION_DENIED)
    return cast(User, actor)


def is_action_available(action_id: str, actor: User | None) -> bool:
    """Return whether the actor may perform the requested system action."""

    return _has_action_permission(get_action_definition(action_id), actor)


async def prepare_action(
    db: AsyncSession,
    *,
    action_id: str,
    parameters: dict[str, Any],
    uid: str,
    actor_uid: str | None = None,
) -> PreparedAction:
    """Validate and prepare a system action without executing it."""

    definition = get_action_definition(action_id)
    actor = await resolve_action_actor(db, uid=uid, actor_uid=actor_uid)
    actor = _require_action_permission(definition, actor)
    prepared_parameters = deepcopy(parameters)
    target = await asyncio.to_thread(definition.prepare, prepared_parameters)
    return PreparedAction(
        action_id=action_id,
        parameters=prepared_parameters,
        target=target,
        actor_uid=actor.uid,
    )


async def validate_prepared_action(
    db: AsyncSession,
    *,
    prepared: PreparedAction,
    uid: str,
    actor_uid: str | None = None,
) -> None:
    """Revalidate permissions and the target of a prepared action."""

    definition = get_action_definition(prepared.action_id)
    current_actor = await resolve_action_actor(db, uid=uid, actor_uid=actor_uid)
    _require_action_permission(definition, current_actor)
    original_actor = await user_crud.get_by_uid(db, prepared.actor_uid)
    _require_action_permission(definition, original_actor)
    current_target = await asyncio.to_thread(definition.prepare, deepcopy(prepared.parameters))
    if current_target != prepared.target:
        raise ParameterException(ERR_SYSTEM_ACTION_TARGET_CHANGED)


def describe_action(prepared: PreparedAction, *, locale: str | None = None) -> str:
    """Render the confirmation description for a prepared action."""

    definition = get_action_definition(prepared.action_id)
    return t(definition.description_key, locale=locale, **prepared.parameters)


def requires_action_confirmation(action_id: str, cfg: ProfileConfig) -> bool:
    """Return whether the active profile requires confirmation for an action."""

    definition = get_action_definition(action_id)
    return definition.confirm_with_profile and cfg.security.audit_threshold > 0


async def execute_action(
    db: AsyncSession,
    *,
    prepared: PreparedAction,
    uid: str,
    cfg: ProfileConfig,
    approved: bool = False,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    """Revalidate and execute a previously prepared system action."""

    definition = get_action_definition(prepared.action_id)
    await validate_prepared_action(db, prepared=prepared, uid=uid, actor_uid=actor_uid)
    if requires_action_confirmation(prepared.action_id, cfg) and not approved:
        raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED)
    return await asyncio.to_thread(definition.execute, prepared.parameters, prepared.target)
