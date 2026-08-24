"""长期记忆 Profile 嵌入模型预览、确认和运行时状态服务。"""

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps, sha256_text
from app.core.constants import (
    ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED,
    ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED,
    ERR_PROFILE_MEMORY_EMBEDDING_PROBE_FAILED,
    ERR_PROFILE_MEMORY_MIGRATION_ACTIVE,
    ERR_PROFILE_MEMORY_MIGRATION_CONFLICT,
    ERR_PROFILE_MEMORY_SELECTION_EXPIRED,
    ERR_PROFILE_MEMORY_SELECTION_INVALID,
    ERR_PROFILE_MEMORY_SELECTION_STALE,
    ERR_PROFILE_NOT_FOUND,
)
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_embedding_selection_token_crud,
    memory_record_crud,
    memory_store_crud,
)
from app.core.crud.profile import profile_crud
from app.core.embedding.common import detect_embedding_dimensions, load_embedding_runtime_config
from app.core.exceptions import ParameterException, ResourceNotFoundException
from app.core.memory.identifiers import build_memory_collection_name
from app.core.memory_jobs.manager import memory_job_manager
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryStore,
)
from app.models.profile import LongTermMemoryConfig, Profile, ProfileConfig, ProfileMemoryRuntime

TOKEN_TTL = timedelta(minutes=10)


def _profile_digest(profile: Profile) -> str:
    payload = {
        "id": profile.id,
        "uid": profile.uid,
        "name": profile.name,
        "prompt_id": profile.prompt_id,
        "configs": ProfileConfig.model_validate(profile.configs or {}).model_dump(mode="json"),
        "is_default": profile.is_default,
    }
    return sha256_text(canonical_json_dumps(payload))


def build_embedding_signature(channel_id: int, model_id: str, dimensions: int) -> str:
    return sha256_text(canonical_json_dumps({"channel_id": channel_id, "model_id": model_id, "dimensions": dimensions}))


def _memory_configs(profile: Profile) -> ProfileConfig:
    return ProfileConfig.model_validate(profile.configs or {})


def _migration_is_active(store: LongTermMemoryStore | None) -> bool:
    if store is None or store.migration_job_id is None:
        return False
    return store.migration_status not in {
        LongTermMemoryMigrationStatus.SUCCEEDED,
        LongTermMemoryMigrationStatus.FAILED,
        LongTermMemoryMigrationStatus.CANCELLED,
        None,
    }


def _is_memory_confirmation_unique_integrity_error(exc: IntegrityError) -> bool:
    known_constraints = {
        "uq_long_term_memory_store_uid",
        "uq_long_term_memory_embedding_revision_uid_revision",
    }
    original = getattr(exc, "orig", None)
    constraint_name = str(getattr(original, "constraint_name", None) or getattr(exc, "constraint_name", None) or "").lower()
    detail = " ".join(part.lower() for part in (str(original or ""), str(exc)))
    if constraint_name in known_constraints or any(name in detail for name in known_constraints):
        return True
    is_unique_violation = any(marker in detail for marker in ("unique", "duplicate key", "duplicate entry"))
    if not is_unique_violation:
        return False
    is_store_uid_conflict = "long_term_memory_store" in detail and "uid" in detail
    is_revision_conflict = "long_term_memory_embedding_revision" in detail and "uid" in detail and "revision" in detail
    return is_store_uid_conflict or is_revision_conflict


async def _preview_embedding_selection(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
    embedding_channel_id: int,
    embedding_model_id: str,
) -> dict[str, Any]:
    profile = await profile_crud.get_snapshot(db, profile_id)
    if profile is None or profile.uid != uid:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
    if store is not None and store.active_embedding_revision > 0:
        memory_config = _memory_configs(profile).memory
        if memory_config.embedding_channel_id not in {None, store.active_embedding_channel_id} or memory_config.embedding_model_id not in {None, store.active_embedding_model_id}:
            raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)
    profile_digest = _profile_digest(profile)
    active_revision = store.active_embedding_revision if store else 0

    runtime_config = await load_embedding_runtime_config(db, embedding_channel_id, embedding_model_id)
    await db.commit()
    try:
        dimensions = await detect_embedding_dimensions(runtime_config)
    except Exception as exc:
        raise ParameterException(ERR_PROFILE_MEMORY_EMBEDDING_PROBE_FAILED, code=502) from exc

    current_profile = await profile_crud.get_snapshot(db, profile_id)
    current_store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
    if current_profile is None or current_profile.uid != uid or _profile_digest(current_profile) != profile_digest or (current_store.active_embedding_revision if current_store else 0) != active_revision:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_STALE)

    target_embedding_signature = build_embedding_signature(embedding_channel_id, embedding_model_id, dimensions)
    token = secrets.token_urlsafe(32)
    expires_at = get_local_time() + TOKEN_TTL
    active_snapshot = {
        "channel_id": current_store.active_embedding_channel_id if current_store else None,
        "model_id": current_store.active_embedding_model_id if current_store else None,
        "dimensions": current_store.active_embedding_dimensions if current_store else None,
        "revision": active_revision,
    }
    is_initial_selection = not bool(current_store and current_store.active_embedding_revision)
    estimated_record_count = await memory_record_crud.count_active(db, uid=uid)
    await memory_embedding_selection_token_crud.create(
        db,
        uid=uid,
        profile_id=profile_id,
        token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        profile_config_digest=profile_digest,
        active_embedding_revision=active_revision,
        target_embedding_channel_id=embedding_channel_id,
        target_embedding_model_id=embedding_model_id,
        target_embedding_dimensions=dimensions,
        target_embedding_signature=target_embedding_signature,
        expires_at=expires_at,
        commit=False,
    )
    await db.commit()
    return {
        "embedding_selection_signature": token,
        "channel_name": runtime_config.channel_name,
        "channel_id": embedding_channel_id,
        "model_id": embedding_model_id,
        "dimensions": dimensions,
        "actual_dimensions": dimensions,
        "current_active": active_snapshot,
        "is_initial_selection": is_initial_selection,
        "estimated_record_count": estimated_record_count,
        "expires_at": expires_at,
    }


async def preview_embedding_selection(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
    embedding_channel_id: int,
    embedding_model_id: str,
) -> dict[str, Any]:
    try:
        return await _preview_embedding_selection(
            db,
            uid=uid,
            profile_id=profile_id,
            embedding_channel_id=embedding_channel_id,
            embedding_model_id=embedding_model_id,
        )
    except Exception:
        await db.rollback()
        raise


def _profile_configs_with_active_memory(
    profile: Profile,
    memory: LongTermMemoryConfig,
    store: LongTermMemoryStore,
) -> dict[str, Any]:
    configs = ProfileConfig.model_validate(profile.configs or {}).model_dump()
    current_memory = configs["memory"]
    memory_data = memory.model_dump(exclude_unset=True)
    for field in ("chat_history", "knowledge"):
        nested_data = memory_data.pop(field, None)
        if nested_data is not None:
            current_memory[field] = {**current_memory[field], **nested_data}
    current_memory.update(memory_data)
    current_memory["embedding_channel_id"] = store.active_embedding_channel_id
    current_memory["embedding_model_id"] = store.active_embedding_model_id
    configs["memory"] = current_memory
    return ProfileConfig.model_validate(configs).model_dump()


async def _confirm_embedding_selection(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
    memory: LongTermMemoryConfig,
    embedding_selection_signature: str,
) -> tuple[Profile, LongTermMemoryStore]:
    profile = await profile_crud.get_snapshot(db, profile_id)
    if profile is None or profile.uid != uid:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
    token_digest = hashlib.sha256(embedding_selection_signature.encode("utf-8")).hexdigest()
    selection = await memory_embedding_selection_token_crud.get_by_digest(
        db,
        uid=uid,
        profile_id=profile_id,
        token_digest=token_digest,
    )
    now = get_local_time()
    if selection is None or selection.consumed_at is not None:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_INVALID)
    if selection.target_embedding_signature != build_embedding_signature(
        selection.target_embedding_channel_id,
        selection.target_embedding_model_id,
        selection.target_embedding_dimensions,
    ):
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_INVALID)
    expires_at = selection.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if expires_at <= now:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_EXPIRED)
    if selection.profile_config_digest != _profile_digest(profile) or selection.active_embedding_revision != (store.active_embedding_revision if store else 0):
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_STALE)

    if memory.embedding_channel_id is not None and memory.embedding_channel_id != selection.target_embedding_channel_id:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_STALE)
    if memory.embedding_model_id is not None and memory.embedding_model_id != selection.target_embedding_model_id:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_STALE)
    memory.embedding_channel_id = selection.target_embedding_channel_id
    memory.embedding_model_id = selection.target_embedding_model_id

    current_profile = await profile_crud.get_snapshot(db, profile_id)
    current_store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
    if current_profile is None or current_profile.uid != uid or _profile_digest(current_profile) != selection.profile_config_digest or (current_store.active_embedding_revision if current_store else 0) != selection.active_embedding_revision:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_STALE)
    if current_store is None:
        current_store = await memory_store_crud.create(db, uid=uid, commit=False)
    if current_store is None:
        raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)
    profile = current_profile

    if current_store.active_embedding_revision > 0 and not all(
        value is not None
        for value in (
            current_store.active_embedding_channel_id,
            current_store.active_embedding_model_id,
            current_store.active_embedding_dimensions,
            current_store.active_embedding_signature,
            current_store.active_collection_name,
        )
    ):
        raise ParameterException(ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED)

    target_matches_active = (
        current_store.active_embedding_channel_id == selection.target_embedding_channel_id
        and current_store.active_embedding_model_id == selection.target_embedding_model_id
        and current_store.active_embedding_dimensions == selection.target_embedding_dimensions
        and current_store.active_embedding_signature == selection.target_embedding_signature
    )
    await load_embedding_runtime_config(
        db,
        selection.target_embedding_channel_id,
        selection.target_embedding_model_id,
        lock_for_reference_write=True,
    )
    consumed = await memory_embedding_selection_token_crud.consume_if_available(
        db,
        uid=uid,
        profile_id=profile_id,
        token_digest=token_digest,
        consumed_at=now,
        commit=False,
    )
    if consumed is None:
        raise ParameterException(ERR_PROFILE_MEMORY_SELECTION_INVALID)

    if current_store.active_embedding_revision == 0:
        collection_name = build_memory_collection_name(uid, selection.target_embedding_signature, 1, "active")
        current_store = await memory_store_crud.activate_initial_embedding_if_unconfigured(
            db,
            uid=uid,
            expected_active_revision=selection.active_embedding_revision,
            active_embedding_channel_id=selection.target_embedding_channel_id,
            active_embedding_model_id=selection.target_embedding_model_id,
            active_embedding_dimensions=selection.target_embedding_dimensions,
            active_embedding_signature=selection.target_embedding_signature,
            active_collection_name=collection_name,
            commit=False,
        )
        if current_store is None:
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_CONFLICT)
        await profile_crud.update(
            db,
            db_obj=profile,
            obj_in={"configs": _profile_configs_with_active_memory(profile, memory, current_store)},
            commit=False,
        )
        await profile_crud.normalize_memory_selection_by_uid(
            db,
            uid=uid,
            embedding_channel_id=current_store.active_embedding_channel_id,
            embedding_model_id=current_store.active_embedding_model_id,
            commit=False,
        )
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=1,
            commit=False,
            from_channel_id=None,
            from_model_id=None,
            from_dimensions=None,
            from_signature=None,
            from_collection=None,
            to_channel_id=selection.target_embedding_channel_id,
            to_model_id=selection.target_embedding_model_id,
            to_dimensions=selection.target_embedding_dimensions,
            to_signature=selection.target_embedding_signature,
            to_collection=collection_name,
            confirmation_source_profile_id=profile_id,
            embedding_selection_signature=token_digest,
            confirmed_at=now,
            status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
            finished_at=now,
        )
    elif target_matches_active:
        await profile_crud.update(
            db,
            db_obj=profile,
            obj_in={"configs": _profile_configs_with_active_memory(profile, memory, current_store)},
            commit=False,
        )
        await profile_crud.normalize_memory_selection_by_uid(
            db,
            uid=uid,
            embedding_channel_id=current_store.active_embedding_channel_id,
            embedding_model_id=current_store.active_embedding_model_id,
            commit=False,
        )
    else:
        if current_store.index_status == LongTermMemoryIndexStatus.REINDEXING:
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_CONFLICT)
        if _migration_is_active(current_store):
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_ACTIVE)
        if current_store.old_collection_cleanup_status in {
            LongTermMemoryOldCollectionCleanupStatus.PENDING,
            LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            LongTermMemoryOldCollectionCleanupStatus.FAILED,
        }:
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_CONFLICT)
        next_revision = await memory_embedding_revision_crud.get_next_revision(db, uid=uid)
        collection_name = build_memory_collection_name(uid, selection.target_embedding_signature, next_revision, "target")
        submission = await memory_job_manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key=f"embedding-migration:{token_digest}",
            payload={
                "target": {
                    "channel_id": selection.target_embedding_channel_id,
                    "model_id": selection.target_embedding_model_id,
                    "dimensions": selection.target_embedding_dimensions,
                    "signature": selection.target_embedding_signature,
                    "collection": collection_name,
                    "revision": next_revision,
                },
                "from": {
                    "channel_id": current_store.active_embedding_channel_id,
                    "model_id": current_store.active_embedding_model_id,
                    "dimensions": current_store.active_embedding_dimensions,
                    "signature": current_store.active_embedding_signature,
                    "collection": current_store.active_collection_name,
                    "revision": current_store.active_embedding_revision,
                },
            },
            source_profile_id=profile_id,
            commit=False,
        )
        job = submission.job
        current_store = await memory_store_crud.start_embedding_migration(
            db,
            uid=uid,
            job_id=job.id,
            expected_active_revision=current_store.active_embedding_revision,
            target_embedding_channel_id=selection.target_embedding_channel_id,
            target_embedding_model_id=selection.target_embedding_model_id,
            target_embedding_dimensions=selection.target_embedding_dimensions,
            target_embedding_signature=selection.target_embedding_signature,
            target_collection_name=collection_name,
            migration_started_at=now,
            commit=False,
        )
        if current_store is None:
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_CONFLICT)
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=next_revision,
            commit=False,
            from_channel_id=current_store.active_embedding_channel_id,
            from_model_id=current_store.active_embedding_model_id,
            from_dimensions=current_store.active_embedding_dimensions,
            from_signature=current_store.active_embedding_signature,
            from_collection=current_store.active_collection_name,
            to_channel_id=selection.target_embedding_channel_id,
            to_model_id=selection.target_embedding_model_id,
            to_dimensions=selection.target_embedding_dimensions,
            to_signature=selection.target_embedding_signature,
            to_collection=collection_name,
            confirmation_source_profile_id=profile_id,
            embedding_selection_signature=token_digest,
            confirmed_at=now,
            job_id=job.id,
            status=LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
        )
        await profile_crud.update(
            db,
            db_obj=profile,
            obj_in={"configs": _profile_configs_with_active_memory(profile, memory, current_store)},
            commit=False,
        )
        await profile_crud.normalize_memory_selection_by_uid(
            db,
            uid=uid,
            embedding_channel_id=current_store.active_embedding_channel_id,
            embedding_model_id=current_store.active_embedding_model_id,
            commit=False,
        )

    await db.commit()
    return profile, current_store


async def confirm_embedding_selection(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
    memory: LongTermMemoryConfig,
    embedding_selection_signature: str,
) -> tuple[Profile, LongTermMemoryStore]:
    try:
        return await _confirm_embedding_selection(
            db,
            uid=uid,
            profile_id=profile_id,
            memory=memory,
            embedding_selection_signature=embedding_selection_signature,
        )
    except IntegrityError as exc:
        await db.rollback()
        if _is_memory_confirmation_unique_integrity_error(exc):
            raise ParameterException(ERR_PROFILE_MEMORY_MIGRATION_CONFLICT) from exc
        raise
    except Exception:
        await db.rollback()
        raise


def build_memory_runtime(profile: Profile, store: LongTermMemoryStore | None) -> ProfileMemoryRuntime:
    memory_config = _memory_configs(profile).memory
    if store is None:
        return ProfileMemoryRuntime(enabled=memory_config.enabled)
    return ProfileMemoryRuntime(
        enabled=memory_config.enabled,
        embedding_channel_id=store.active_embedding_channel_id,
        embedding_model_id=store.active_embedding_model_id,
        embedding_dimensions=store.active_embedding_dimensions,
        embedding_signature=store.active_embedding_signature,
        embedding_revision=store.active_embedding_revision,
        active_collection_name=store.active_collection_name,
        target_embedding_channel_id=store.target_embedding_channel_id,
        target_embedding_model_id=store.target_embedding_model_id,
        target_embedding_dimensions=store.target_embedding_dimensions,
        target_embedding_signature=store.target_embedding_signature,
        migration_status=getattr(store.migration_status, "value", store.migration_status),
        migration_job_id=store.migration_job_id,
    )


def normalize_profile_memory_config(configs: dict[str, Any], store: LongTermMemoryStore | None) -> dict[str, Any]:
    profile_config = ProfileConfig.model_validate(configs)
    memory = profile_config.memory
    if store is None or store.active_embedding_revision == 0:
        if memory.enabled:
            raise ParameterException(ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED)
        if memory.embedding_channel_id is not None or memory.embedding_model_id is not None:
            raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)
        memory.embedding_channel_id = None
        memory.embedding_model_id = None
        profile_config.memory = memory
        return profile_config.model_dump()
    if not all(
        value is not None
        for value in (
            store.active_embedding_channel_id,
            store.active_embedding_model_id,
            store.active_embedding_dimensions,
            store.active_embedding_signature,
            store.active_collection_name,
        )
    ):
        raise ParameterException(ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED)
    if memory.embedding_channel_id is not None and memory.embedding_channel_id != store.active_embedding_channel_id:
        raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)
    if memory.embedding_model_id is not None and memory.embedding_model_id != store.active_embedding_model_id:
        raise ParameterException(ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED)
    memory.embedding_channel_id = store.active_embedding_channel_id
    memory.embedding_model_id = store.active_embedding_model_id
    profile_config.memory = memory
    return profile_config.model_dump()


def normalize_profile_memory_for_update(profile: Profile, configs: dict[str, Any], store: LongTermMemoryStore | None) -> dict[str, Any]:
    return normalize_profile_memory_config(configs, store)


def normalize_profile_memory_for_create(configs: dict[str, Any], store: LongTermMemoryStore | None) -> dict[str, Any]:
    return normalize_profile_memory_config(configs, store)
