"""长期记忆对管理员渠道和模型身份的保护检查。"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.memory import memory_reference_crud
from app.models.channel import ModelUsage
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)


@dataclass(frozen=True, slots=True)
class LongTermMemoryChannelModelReference:
    uid: str
    channel_id: int
    model_id: str | None
    usage: str


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _as_channel_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_model_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    model_id = value.strip()
    return model_id or None


def _payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _payload_pairs(payload: object) -> list[tuple[int, str | None]]:
    if not isinstance(payload, dict):
        return []

    pairs: set[tuple[int, str | None]] = set()

    def add_pair(data: object, channel_keys: tuple[str, ...], model_keys: tuple[str, ...]) -> None:
        if not isinstance(data, dict):
            return
        channel_id = _as_channel_id(_payload_value(data, channel_keys))
        if channel_id is None:
            return
        pairs.add((channel_id, _as_model_id(_payload_value(data, model_keys))))

    pair_key_groups = (
        (
            (
                "channel_id",
                "embedding_channel_id",
                "active_channel_id",
                "active_embedding_channel_id",
                "source_channel_id",
                "source_embedding_channel_id",
            ),
            (
                "model_id",
                "embedding_model_id",
                "active_model_id",
                "active_embedding_model_id",
                "source_model_id",
                "source_embedding_model_id",
            ),
        ),
        (("from_channel_id", "from_embedding_channel_id"), ("from_model_id", "from_embedding_model_id")),
        (("to_channel_id", "to_embedding_channel_id"), ("to_model_id", "to_embedding_model_id")),
        (("target_channel_id", "target_embedding_channel_id"), ("target_model_id", "target_embedding_model_id")),
        (("old_channel_id", "old_embedding_channel_id"), ("old_model_id", "old_embedding_model_id")),
        (("new_channel_id", "new_embedding_channel_id"), ("new_model_id", "new_embedding_model_id")),
    )
    for channel_keys, model_keys in pair_key_groups:
        add_pair(payload, channel_keys, model_keys)

    for key in (
        "from",
        "to",
        "target",
        "old",
        "new",
        "source",
        "embedding",
        "from_embedding",
        "to_embedding",
        "target_embedding",
        "old_embedding",
        "new_embedding",
        "old_collection",
        "migration",
    ):
        nested = payload.get(key)
        if isinstance(nested, dict):
            add_pair(nested, pair_key_groups[0][0], pair_key_groups[0][1])

    return list(pairs)


def _organization_payload_pairs(payload: object) -> list[tuple[int, str | None]]:
    if not isinstance(payload, dict):
        return []

    pairs: set[tuple[int, str | None]] = set()
    for key in ("organization_model", "model_config"):
        model_config = payload.get(key)
        if not isinstance(model_config, dict):
            continue
        channel_id = _as_channel_id(model_config.get("channel_id"))
        if channel_id is not None:
            pairs.add((channel_id, _as_model_id(model_config.get("model_id"))))
    return list(pairs)


def _add_reference(
    references: set[tuple[str, int, str | None, str]],
    *,
    uid: str,
    raw_channel_id: object,
    raw_model_id: object,
    channel_id: int | None,
    usage: str,
) -> None:
    current_channel_id = _as_channel_id(raw_channel_id)
    if current_channel_id is None or (channel_id is not None and current_channel_id != channel_id):
        return
    references.add((uid, current_channel_id, _as_model_id(raw_model_id), usage))


def _group_by_uid(items: list[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for item in items:
        grouped.setdefault(item.uid, []).append(item)
    return grouped


async def list_memory_channel_references(
    db: AsyncSession,
    *,
    channel_id: int | None = None,
) -> list[LongTermMemoryChannelModelReference]:
    stores = await memory_reference_crud.list_all_stores_for_admin(db)
    stores_by_uid = {store.uid: store for store in stores}
    revisions_by_uid = _group_by_uid(await memory_reference_crud.list_all_embedding_revisions_for_admin(db))
    jobs_by_uid = _group_by_uid(await memory_reference_crud.list_all_memory_jobs_for_admin(db))
    references: set[tuple[str, int, str | None, str]] = set()
    cleanup_statuses = {"pending", "running", "failed"}
    embedding_migration_statuses = {
        LongTermMemoryMutationStatus.PENDING.value,
        LongTermMemoryMutationStatus.RUNNING.value,
        LongTermMemoryMutationStatus.RETRY.value,
        LongTermMemoryMutationStatus.FAILED.value,
    }
    organization_job_statuses = {
        LongTermMemoryMutationStatus.PENDING.value,
        LongTermMemoryMutationStatus.RUNNING.value,
        LongTermMemoryMutationStatus.RETRY.value,
    }

    for uid in sorted(stores_by_uid.keys() | revisions_by_uid.keys() | jobs_by_uid.keys()):
        store = stores_by_uid.get(uid)
        revisions = revisions_by_uid.get(uid, [])
        jobs = jobs_by_uid.get(uid, [])
        if store is None:
            for job in jobs:
                if _value(job.operation) == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION.value and _value(job.status) in embedding_migration_statuses:
                    for current_channel_id, model_id in _payload_pairs(job.payload):
                        _add_reference(
                            references,
                            uid=uid,
                            raw_channel_id=current_channel_id,
                            raw_model_id=model_id,
                            channel_id=channel_id,
                            usage=ModelUsage.EMBEDDING.value,
                        )
                if _value(job.operation) == LongTermMemoryMutationOperation.ORGANIZE.value and _value(job.status) in organization_job_statuses:
                    for current_channel_id, model_id in _organization_payload_pairs(job.payload):
                        _add_reference(
                            references,
                            uid=uid,
                            raw_channel_id=current_channel_id,
                            raw_model_id=model_id,
                            channel_id=channel_id,
                            usage=ModelUsage.CHAT.value,
                        )
            continue

        _add_reference(
            references,
            uid=uid,
            raw_channel_id=store.active_embedding_channel_id,
            raw_model_id=store.active_embedding_model_id,
            channel_id=channel_id,
            usage=ModelUsage.EMBEDDING.value,
        )
        _add_reference(
            references,
            uid=uid,
            raw_channel_id=store.target_embedding_channel_id,
            raw_model_id=store.target_embedding_model_id,
            channel_id=channel_id,
            usage=ModelUsage.EMBEDDING.value,
        )
        _add_reference(
            references,
            uid=uid,
            raw_channel_id=store.organization_channel_id,
            raw_model_id=store.organization_model_id,
            channel_id=channel_id,
            usage=ModelUsage.CHAT.value,
        )

        for job in jobs:
            if _value(job.operation) == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION.value and _value(job.status) in embedding_migration_statuses:
                for current_channel_id, model_id in _payload_pairs(job.payload):
                    _add_reference(
                        references,
                        uid=uid,
                        raw_channel_id=current_channel_id,
                        raw_model_id=model_id,
                        channel_id=channel_id,
                        usage=ModelUsage.EMBEDDING.value,
                    )
            if _value(job.operation) == LongTermMemoryMutationOperation.ORGANIZE.value and _value(job.status) in organization_job_statuses:
                for current_channel_id, model_id in _organization_payload_pairs(job.payload):
                    _add_reference(
                        references,
                        uid=uid,
                        raw_channel_id=current_channel_id,
                        raw_model_id=model_id,
                        channel_id=channel_id,
                        usage=ModelUsage.CHAT.value,
                    )

        if _value(store.old_collection_cleanup_status) not in cleanup_statuses:
            continue

        cleanup_job_ids = {job_id for job_id in (store.migration_job_id, store.old_collection_cleanup_job_id) if isinstance(job_id, int) and not isinstance(job_id, bool)}
        for job in jobs:
            if job.id in cleanup_job_ids:
                for current_channel_id, model_id in _payload_pairs(job.payload):
                    _add_reference(
                        references,
                        uid=uid,
                        raw_channel_id=current_channel_id,
                        raw_model_id=model_id,
                        channel_id=channel_id,
                        usage=ModelUsage.EMBEDDING.value,
                    )

        exact_revisions = [revision for revision in revisions if store.old_collection_name and store.old_collection_name in {revision.from_collection, revision.to_collection}]
        related_revisions = exact_revisions
        if not related_revisions and channel_id is not None:
            related_revisions = [revision for revision in revisions if revision.from_channel_id == channel_id or revision.to_channel_id == channel_id]
        elif not related_revisions:
            related_revisions = revisions

        for revision in related_revisions:
            _add_reference(
                references,
                uid=uid,
                raw_channel_id=revision.from_channel_id,
                raw_model_id=revision.from_model_id,
                channel_id=channel_id,
                usage=ModelUsage.EMBEDDING.value,
            )
            _add_reference(
                references,
                uid=uid,
                raw_channel_id=revision.to_channel_id,
                raw_model_id=revision.to_model_id,
                channel_id=channel_id,
                usage=ModelUsage.EMBEDDING.value,
            )

    return [LongTermMemoryChannelModelReference(uid=uid, channel_id=current_channel_id, model_id=model_id, usage=usage) for uid, current_channel_id, model_id, usage in references]
