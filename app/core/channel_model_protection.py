"""渠道模型引用保护检查。"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KB_CHANNEL_IN_USE,
    ERR_KB_MODEL_IDENTITY_IN_USE,
    ERR_MEMORY_CHANNEL_IN_USE,
    ERR_MEMORY_MODEL_IDENTITY_IN_USE,
)
from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.exceptions import ParameterException
from app.core.memory_channel_protection import list_memory_channel_references
from app.models.channel import ModelUsage


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _model_key(item: dict[str, Any]) -> tuple[object, object] | None:
    if not isinstance(item, dict):
        return None
    model_id = item.get("model_id")
    usage = _value(item.get("usage"))
    if not isinstance(model_id, str) or not model_id or usage is None:
        return None
    return model_id, usage


def _identity(item: dict[str, Any]) -> tuple[object, object, object]:
    return (
        _value(item.get("usage")),
        _value(item.get("protocol")),
        item.get("embedding_dimensions"),
    )


def find_model_identity_update_conflict(
    referenced_model_ids: set[str],
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
) -> str | None:
    old_models = {key: item for item in old_model_ids if (key := _model_key(item)) is not None and key[1] == ModelUsage.EMBEDDING.value}
    new_models = {key: item for item in new_model_ids if (key := _model_key(item)) is not None and key[1] == ModelUsage.EMBEDDING.value}

    for model_id in sorted(referenced_model_ids):
        model_key = (model_id, ModelUsage.EMBEDDING.value)
        old_model = old_models.get(model_key)
        new_model = new_models.get(model_key)
        if old_model is None or new_model is None or _identity(old_model) != _identity(new_model):
            return model_id
    return None


async def assert_channel_model_identity_update_allowed(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
) -> None:
    memory_references = await list_memory_channel_references(db, channel_id=channel_id)
    memory_model_ids = {reference.model_id for reference in memory_references if reference.model_id is not None}
    conflict_model_id = find_model_identity_update_conflict(memory_model_ids, old_model_ids, new_model_ids)
    if conflict_model_id is not None:
        raise ParameterException(ERR_MEMORY_MODEL_IDENTITY_IN_USE, model_id=conflict_model_id)

    knowledge_bases = await knowledge_base_crud.list_by_embedding_channel_id(db, embedding_channel_id=channel_id)
    knowledge_base_model_ids = {knowledge_base.embedding_model_id for knowledge_base in knowledge_bases}
    conflict_model_id = find_model_identity_update_conflict(knowledge_base_model_ids, old_model_ids, new_model_ids)
    if conflict_model_id is not None:
        raise ParameterException(ERR_KB_MODEL_IDENTITY_IN_USE, model_id=conflict_model_id)


async def assert_channel_not_referenced(db: AsyncSession, channel_id: int) -> None:
    if await list_memory_channel_references(db, channel_id=channel_id):
        raise ParameterException(ERR_MEMORY_CHANNEL_IN_USE)

    if await knowledge_base_crud.list_by_embedding_channel_id(db, embedding_channel_id=channel_id):
        raise ParameterException(ERR_KB_CHANNEL_IN_USE)
