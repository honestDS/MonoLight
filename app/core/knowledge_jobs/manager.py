from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
    ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
    ERR_KNOWLEDGE_JOB_FIELD_INVALID,
    ERR_KNOWLEDGE_JOB_FIELD_REQUIRED,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
)
from app.core.crud.knowledge_job import knowledge_job_crud
from app.core.crud.managed_knowledge import managed_knowledge_item_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.knowledge.managed import managed_knowledge_service, normalize_managed_knowledge_key
from app.core.knowledge.managed_container import get_or_create_managed_knowledge_base
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeJob,
    KnowledgeJobOperation,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeSourceType,
)
from app.providers.database.time import get_database_time

_ACTIVE_CHANGE_KEY_CONSTRAINT = "uq_knowledge_job_uid_active_change"


def _is_active_change_key_integrity_error(exc: IntegrityError) -> bool:
    return is_unique_constraint_violation(
        exc,
        constraint_names=(_ACTIVE_CHANGE_KEY_CONSTRAINT,),
        fallback_marker_groups=(("active_change_key",),),
    )


class KnowledgeJobError(BaseBusinessException):
    def __init__(self, message: str, code: int = 500, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class KnowledgeJobValidationError(KnowledgeJobError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message=message, code=400, **kwargs)


class KnowledgeJobConflictError(KnowledgeJobError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message=message, code=409, **kwargs)


class KnowledgeJobTargetBusyError(KnowledgeJobConflictError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeJobSubmissionResult:
    status: ManagedKnowledgeMutationStatus
    item: ManagedKnowledgeItem | None
    job: KnowledgeJob | None
    created: bool


@dataclass(frozen=True, slots=True)
class ProfileKnowledgeJobSubmissionResult:
    knowledge_base: KnowledgeBase
    knowledge_base_created: bool
    status: ManagedKnowledgeMutationStatus
    item: ManagedKnowledgeItem | None
    job: KnowledgeJob | None
    created: bool


def _require_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_REQUIRED, field=field))
    return value.strip()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field=field))
    return value


def _request_hash(operation: KnowledgeJobOperation, request: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            {"operation": operation.value, **request},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="request")) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key_digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _active_key(knowledge_base_id: int, *, knowledge_id: int | None = None, knowledge_key: str | None = None) -> str:
    if knowledge_id is not None:
        return f"managed:{knowledge_base_id}:id:{knowledge_id}"
    if knowledge_key is None:
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_REQUIRED, field="knowledge_key"))
    return f"managed:{knowledge_base_id}:key:{_key_digest(knowledge_key)}"


def _safe_payload(*, content: str | None = None, knowledge_key: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if content is not None:
        payload["content_length"] = len(content)
        payload["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if knowledge_key is not None:
        payload["knowledge_key_hash"] = _key_digest(knowledge_key)
    return payload


class KnowledgeJobManager:
    async def submit_create_for_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        knowledge_key: str,
        content: str,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        dedupe_key: str,
        source_reference: dict[str, Any] | None = None,
        llm_maintainable: bool | None = None,
        source_session_id: str | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> ProfileKnowledgeJobSubmissionResult:
        uid = _require_string(uid, field="uid")
        profile_id = _positive_int(profile_id, field="profile_id")
        try:
            container = await get_or_create_managed_knowledge_base(
                db,
                uid=uid,
                profile_id=profile_id,
            )
            if container.knowledge_base.id is None:
                raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            submission = await self.submit_create(
                db,
                uid=uid,
                knowledge_base_id=container.knowledge_base.id,
                knowledge_key=knowledge_key,
                content=content,
                source_type=source_type,
                actor=actor,
                dedupe_key=dedupe_key,
                source_reference=source_reference,
                llm_maintainable=llm_maintainable,
                source_session_id=source_session_id,
                source_profile_id=profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
                commit=False,
            )
            if commit:
                await db.commit()
                await db.refresh(container.knowledge_base)
                if submission.job is not None:
                    await db.refresh(submission.job)
                if submission.item is not None:
                    await db.refresh(submission.item)
            else:
                await db.flush()
            return ProfileKnowledgeJobSubmissionResult(
                knowledge_base=container.knowledge_base,
                knowledge_base_created=container.created,
                status=submission.status,
                item=submission.item,
                job=submission.job,
                created=submission.created,
            )
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def _create_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        operation: KnowledgeJobOperation,
        dedupe_key: str,
        request_hash: str,
        active_change_key: str,
        knowledge_base_id: int,
        knowledge_id: int | None,
        expected_version: int | None,
        payload: dict[str, Any],
        source_session_id: str | None,
        source_profile_id: int | None,
        source_message_id: int | None,
        max_attempts: int,
    ) -> tuple[KnowledgeJob, bool]:
        available_at = await get_database_time(db)
        try:
            job, created = await knowledge_job_crud.create(
                db,
                uid=uid,
                operation=operation,
                dedupe_key=dedupe_key,
                request_hash=request_hash,
                active_change_key=active_change_key,
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=expected_version,
                payload=payload,
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
                available_at=available_at,
                commit=False,
            )
        except IntegrityError as exc:
            if _is_active_change_key_integrity_error(exc):
                raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY)) from exc
            raise
        if not created and (
            job.operation != operation
            or job.request_hash != request_hash
            or job.knowledge_base_id != knowledge_base_id
        ):
            raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT))
        return job, created

    async def _existing_submission(
        self,
        db: AsyncSession,
        *,
        job: KnowledgeJob,
    ) -> KnowledgeJobSubmissionResult:
        item = None
        if job.knowledge_id is not None:
            item = await managed_knowledge_item_crud.get_by_id(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
                knowledge_id=job.knowledge_id,
            )
        return KnowledgeJobSubmissionResult(
            status=ManagedKnowledgeMutationStatus.UNCHANGED,
            item=item,
            job=job,
            created=False,
        )

    async def _bind_job(
        self,
        db: AsyncSession,
        *,
        job: KnowledgeJob,
        item: ManagedKnowledgeItem,
        source_job_id: bool,
    ) -> tuple[KnowledgeJob, ManagedKnowledgeItem]:
        if job.id is None or item.id is None:
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        targeted = await knowledge_job_crud.set_target(
            db,
            uid=job.uid,
            job_id=job.id,
            knowledge_id=item.id,
            expected_version=item.version,
            commit=False,
        )
        if targeted is None:
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        bound = await managed_knowledge_item_crud.bind_pending_job(
            db,
            uid=item.uid,
            knowledge_base_id=item.knowledge_base_id,
            knowledge_id=item.id,
            expected_version=item.version,
            job_id=job.id,
            source_job_id=job.id if source_job_id else None,
            commit=False,
        )
        if bound is None:
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        return targeted, bound

    @staticmethod
    def _needs_publication(item: ManagedKnowledgeItem | None) -> bool:
        return bool(
            item is not None
            and item.deleted_at is None
            and item.pending_job_id is None
            and not item.is_recallable
            and item.indexed_version < item.version
        )

    async def submit_create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_key: str,
        content: str,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        dedupe_key: str,
        source_reference: dict[str, Any] | None = None,
        llm_maintainable: bool | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> KnowledgeJobSubmissionResult:
        uid = _require_string(uid, field="uid")
        knowledge_base_id = _positive_int(knowledge_base_id, field="knowledge_base_id")
        knowledge_key = normalize_managed_knowledge_key(
            _require_string(knowledge_key, field="knowledge_key")
        )
        dedupe_key = _require_string(dedupe_key, field="dedupe_key")
        _positive_int(max_attempts, field="max_attempts")
        operation = KnowledgeJobOperation.MANAGED_CREATE
        request_hash = _request_hash(
            operation,
            {
                "knowledge_base_id": knowledge_base_id,
                "knowledge_key": knowledge_key,
                "content": content,
                "source_type": str(source_type),
                "actor": str(actor),
                "source_reference": source_reference,
                "llm_maintainable": llm_maintainable,
            },
        )
        try:
            job, created = await self._create_job(
                db,
                uid=uid,
                operation=operation,
                dedupe_key=dedupe_key,
                request_hash=request_hash,
                active_change_key=_active_key(knowledge_base_id, knowledge_key=knowledge_key),
                knowledge_base_id=knowledge_base_id,
                knowledge_id=None,
                expected_version=None,
                payload=_safe_payload(content=content, knowledge_key=knowledge_key),
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
            )
            if not created:
                if commit:
                    await db.commit()
                return await self._existing_submission(db, job=job)
            mutation = await managed_knowledge_service.create(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                knowledge_key=knowledge_key,
                content=content,
                source_type=source_type,
                actor=actor,
                source_reference=source_reference,
                source_job_id=job.id,
                llm_maintainable=llm_maintainable,
                commit=False,
            )
            if mutation.status == ManagedKnowledgeMutationStatus.CREATED and mutation.item is not None:
                job, item = await self._bind_job(db, job=job, item=mutation.item, source_job_id=True)
            elif (
                mutation.status == ManagedKnowledgeMutationStatus.EXISTING_KEY
                and mutation.item is not None
                and mutation.item.content == content
                and self._needs_publication(mutation.item)
            ):
                job, item = await self._bind_job(db, job=job, item=mutation.item, source_job_id=False)
            else:
                await knowledge_job_crud.delete_unstarted(db, uid=uid, job_id=job.id, commit=False)
                job = None
                item = mutation.item
            if commit:
                await db.commit()
                if job is not None:
                    await db.refresh(job)
                if item is not None:
                    await db.refresh(item)
            return KnowledgeJobSubmissionResult(
                status=mutation.status,
                item=item,
                job=job,
                created=job is not None,
            )
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def submit_update(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        knowledge_key: str,
        content: str,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        dedupe_key: str,
        source_reference: dict[str, Any] | None = None,
        llm_maintainable: bool | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> KnowledgeJobSubmissionResult:
        uid = _require_string(uid, field="uid")
        knowledge_base_id = _positive_int(knowledge_base_id, field="knowledge_base_id")
        knowledge_id = _positive_int(knowledge_id, field="knowledge_id")
        expected_version = _positive_int(expected_version, field="expected_version")
        knowledge_key = normalize_managed_knowledge_key(
            _require_string(knowledge_key, field="knowledge_key")
        )
        dedupe_key = _require_string(dedupe_key, field="dedupe_key")
        _positive_int(max_attempts, field="max_attempts")
        operation = KnowledgeJobOperation.MANAGED_UPDATE
        request_hash = _request_hash(
            operation,
            {
                "knowledge_base_id": knowledge_base_id,
                "knowledge_id": knowledge_id,
                "expected_version": expected_version,
                "knowledge_key": knowledge_key,
                "content": content,
                "source_type": str(source_type),
                "actor": str(actor),
                "source_reference": source_reference,
                "llm_maintainable": llm_maintainable,
            },
        )
        try:
            job, created = await self._create_job(
                db,
                uid=uid,
                operation=operation,
                dedupe_key=dedupe_key,
                request_hash=request_hash,
                active_change_key=_active_key(knowledge_base_id, knowledge_id=knowledge_id),
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=expected_version,
                payload=_safe_payload(content=content, knowledge_key=knowledge_key),
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
            )
            if not created:
                if commit:
                    await db.commit()
                return await self._existing_submission(db, job=job)
            mutation = await managed_knowledge_service.update(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=expected_version,
                knowledge_key=knowledge_key,
                content=content,
                source_type=source_type,
                actor=actor,
                source_reference=source_reference,
                source_job_id=job.id,
                llm_maintainable=llm_maintainable,
                commit=False,
            )
            if mutation.status == ManagedKnowledgeMutationStatus.UPDATED and mutation.item is not None:
                job, item = await self._bind_job(db, job=job, item=mutation.item, source_job_id=True)
            elif (
                mutation.item is not None
                and mutation.item.id == knowledge_id
                and self._needs_publication(mutation.item)
            ):
                job, item = await self._bind_job(db, job=job, item=mutation.item, source_job_id=False)
            else:
                await knowledge_job_crud.delete_unstarted(db, uid=uid, job_id=job.id, commit=False)
                job = None
                item = mutation.item
            if commit:
                await db.commit()
                if job is not None:
                    await db.refresh(job)
                if item is not None:
                    await db.refresh(item)
            return KnowledgeJobSubmissionResult(mutation.status, item, job, job is not None)
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def submit_delete(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        dedupe_key: str,
        source_reference: dict[str, Any] | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> KnowledgeJobSubmissionResult:
        uid = _require_string(uid, field="uid")
        knowledge_base_id = _positive_int(knowledge_base_id, field="knowledge_base_id")
        knowledge_id = _positive_int(knowledge_id, field="knowledge_id")
        expected_version = _positive_int(expected_version, field="expected_version")
        dedupe_key = _require_string(dedupe_key, field="dedupe_key")
        _positive_int(max_attempts, field="max_attempts")
        operation = KnowledgeJobOperation.MANAGED_DELETE_CLEANUP
        request_hash = _request_hash(
            operation,
            {
                "knowledge_base_id": knowledge_base_id,
                "knowledge_id": knowledge_id,
                "expected_version": expected_version,
                "source_type": str(source_type),
                "actor": str(actor),
                "source_reference": source_reference,
            },
        )
        try:
            job, created = await self._create_job(
                db,
                uid=uid,
                operation=operation,
                dedupe_key=dedupe_key,
                request_hash=request_hash,
                active_change_key=_active_key(knowledge_base_id, knowledge_id=knowledge_id),
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=expected_version,
                payload={},
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
            )
            if not created:
                if commit:
                    await db.commit()
                return await self._existing_submission(db, job=job)
            mutation = await managed_knowledge_service.delete(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=expected_version,
                source_type=source_type,
                actor=actor,
                source_reference=source_reference,
                source_job_id=job.id,
                commit=False,
            )
            if mutation.item is not None and (
                mutation.status == ManagedKnowledgeMutationStatus.DELETED
                or (mutation.item.deleted_at is not None and mutation.item.pending_job_id is None)
            ):
                job, item = await self._bind_job(
                    db,
                    job=job,
                    item=mutation.item,
                    source_job_id=mutation.status == ManagedKnowledgeMutationStatus.DELETED,
                )
            else:
                await knowledge_job_crud.delete_unstarted(db, uid=uid, job_id=job.id, commit=False)
                job = None
                item = mutation.item
            if commit:
                await db.commit()
                if job is not None:
                    await db.refresh(job)
                if item is not None:
                    await db.refresh(item)
            return KnowledgeJobSubmissionResult(mutation.status, item, job, job is not None)
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise


knowledge_job_manager = KnowledgeJobManager()


__all__ = [
    "KnowledgeJobConflictError",
    "KnowledgeJobError",
    "KnowledgeJobManager",
    "KnowledgeJobSubmissionResult",
    "ProfileKnowledgeJobSubmissionResult",
    "KnowledgeJobTargetBusyError",
    "KnowledgeJobValidationError",
    "knowledge_job_manager",
]
