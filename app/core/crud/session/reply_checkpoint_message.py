from copy import deepcopy
from typing import Any

from sqlalchemy import delete, exists
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    ERR_SESSION_REPLY_CHECKPOINT_INVALID,
    ERR_SESSION_REPLY_WORK_NOT_FOUND,
    SESSION_REPLY_CHECKPOINT_BATCH_SIZE,
)
from app.core.i18n import t
from app.core.utils.dispatcher.checkpoint_message import CheckpointMessagePayload, checkpoint_message_hash
from app.models.session_reply_work_item import (
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplyWorkItem,
)


class CRUDSessionReplyCheckpointMessage:
    async def save_groups(
        self,
        db: AsyncSession,
        work_id: int,
        kind: str,
        groups: list[list[dict[str, Any]]],
    ) -> list[list[int]]:
        # 调用方须在同一事务内持有工作行写权限；本层只写入当前事务，不提交。
        work_result = await db.execute(select(SessionReplyWorkItem.uid, SessionReplyWorkItem.session_id).where(SessionReplyWorkItem.id == work_id))
        work_scope = work_result.one_or_none()
        if work_scope is None:
            raise ValueError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
        uid = work_scope.uid
        session_id = work_scope.session_id

        reference_result = await db.execute(
            select(SessionReplyCheckpointMessageReference.message_id).where(
                SessionReplyCheckpointMessageReference.work_id == work_id,
                SessionReplyCheckpointMessageReference.kind == kind,
            )
        )
        existing_reference_ids = set(reference_result.scalars().all())

        hash_order: list[str] = []
        payload_by_hash: dict[str, dict[str, Any]] = {}
        group_hashes: list[list[str]] = []
        for group in groups:
            current_group_hashes: list[str] = []
            for payload in group:
                if isinstance(payload, CheckpointMessagePayload):
                    content_hash = payload.content_hash
                else:
                    content_hash = checkpoint_message_hash(payload)
                current_group_hashes.append(content_hash)
                if content_hash not in payload_by_hash:
                    payload_by_hash[content_hash] = dict(payload)
                    hash_order.append(content_hash)
            group_hashes.append(current_group_hashes)

        message_id_by_hash: dict[str, int] = {}
        for offset in range(0, len(hash_order), SESSION_REPLY_CHECKPOINT_BATCH_SIZE):
            hash_batch = hash_order[offset : offset + SESSION_REPLY_CHECKPOINT_BATCH_SIZE]
            result = await db.execute(
                select(SessionReplyCheckpointMessage.id, SessionReplyCheckpointMessage.content_hash)
                .where(
                    SessionReplyCheckpointMessage.session_id == session_id,
                    SessionReplyCheckpointMessage.uid == uid,
                    SessionReplyCheckpointMessage.content_hash.in_(hash_batch),
                )
                .order_by(SessionReplyCheckpointMessage.id)
                .with_for_update()
            )
            for row in result:
                if row.id is not None:
                    message_id_by_hash[row.content_hash] = row.id

        missing_hashes = [content_hash for content_hash in hash_order if content_hash not in message_id_by_hash]
        if missing_hashes:
            rows = [
                {
                    "uid": uid,
                    "session_id": session_id,
                    "content_hash": content_hash,
                    "payload": payload_by_hash[content_hash],
                }
                for content_hash in missing_hashes
            ]
            for offset in range(0, len(rows), SESSION_REPLY_CHECKPOINT_BATCH_SIZE):
                row_batch = rows[offset : offset + SESSION_REPLY_CHECKPOINT_BATCH_SIZE]
                dialect_name = db.get_bind().dialect.name
                if dialect_name == "sqlite":
                    statement = (
                        sqlite_insert(SessionReplyCheckpointMessage)
                        .values(row_batch)
                        .on_conflict_do_nothing(
                            index_elements=[
                                SessionReplyCheckpointMessage.session_id,
                                SessionReplyCheckpointMessage.uid,
                                SessionReplyCheckpointMessage.content_hash,
                            ]
                        )
                    )
                elif dialect_name == "mysql":
                    statement = mysql_insert(SessionReplyCheckpointMessage).values(row_batch).on_duplicate_key_update(content_hash=SessionReplyCheckpointMessage.content_hash)
                else:
                    raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))
                await db.execute(statement)

            for offset in range(0, len(missing_hashes), SESSION_REPLY_CHECKPOINT_BATCH_SIZE):
                hash_batch = missing_hashes[offset : offset + SESSION_REPLY_CHECKPOINT_BATCH_SIZE]
                result = await db.execute(
                    select(SessionReplyCheckpointMessage.id, SessionReplyCheckpointMessage.content_hash)
                    .where(
                        SessionReplyCheckpointMessage.session_id == session_id,
                        SessionReplyCheckpointMessage.uid == uid,
                        SessionReplyCheckpointMessage.content_hash.in_(hash_batch),
                    )
                    .order_by(SessionReplyCheckpointMessage.id)
                    .with_for_update()
                )
                for row in result:
                    if row.id is not None:
                        message_id_by_hash[row.content_hash] = row.id

        group_message_ids: list[list[int]] = []
        desired_reference_ids: set[int] = set()
        for current_group_hashes in group_hashes:
            group_ids: list[int] = []
            for content_hash in current_group_hashes:
                message_id = message_id_by_hash.get(content_hash)
                if message_id is None:
                    raise ValueError(t(ERR_SESSION_REPLY_CHECKPOINT_INVALID))
                group_ids.append(message_id)
                desired_reference_ids.add(message_id)
            group_message_ids.append(group_ids)

        ids_to_remove = existing_reference_ids - desired_reference_ids
        if ids_to_remove:
            await db.execute(
                delete(SessionReplyCheckpointMessageReference).where(
                    SessionReplyCheckpointMessageReference.work_id == work_id,
                    SessionReplyCheckpointMessageReference.kind == kind,
                    SessionReplyCheckpointMessageReference.message_id.in_(ids_to_remove),
                )
            )

        ids_to_add = desired_reference_ids - existing_reference_ids
        if ids_to_add:
            for message_id in sorted(ids_to_add):
                db.add(
                    SessionReplyCheckpointMessageReference(
                        work_id=work_id,
                        kind=kind,
                        message_id=message_id,
                    )
                )
            await db.flush()

        return group_message_ids

    async def load_messages(
        self,
        db: AsyncSession,
        work_id: int,
        message_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not message_ids:
            return []

        work_result = await db.execute(select(SessionReplyWorkItem.uid, SessionReplyWorkItem.session_id).where(SessionReplyWorkItem.id == work_id))
        work_scope = work_result.one_or_none()
        if work_scope is None:
            raise ValueError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
        uid = work_scope.uid
        session_id = work_scope.session_id

        unique_message_ids: list[int] = []
        seen_message_ids: set[int] = set()
        for message_id in message_ids:
            if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
                raise ValueError(t(ERR_SESSION_REPLY_CHECKPOINT_INVALID))
            if message_id not in seen_message_ids:
                seen_message_ids.add(message_id)
                unique_message_ids.append(message_id)

        payload_by_id: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(unique_message_ids), SESSION_REPLY_CHECKPOINT_BATCH_SIZE):
            id_batch = unique_message_ids[offset : offset + SESSION_REPLY_CHECKPOINT_BATCH_SIZE]
            result = await db.execute(
                select(SessionReplyCheckpointMessage.id, SessionReplyCheckpointMessage.payload)
                .where(
                    SessionReplyCheckpointMessage.id.in_(id_batch),
                    SessionReplyCheckpointMessage.uid == uid,
                    SessionReplyCheckpointMessage.session_id == session_id,
                )
                .order_by(SessionReplyCheckpointMessage.id)
            )
            for row in result:
                if row.id is not None:
                    payload_by_id[row.id] = row.payload

        for message_id in unique_message_ids:
            if message_id not in payload_by_id:
                raise ValueError(t(ERR_SESSION_REPLY_CHECKPOINT_INVALID))

        loaded_messages: list[dict[str, Any]] = []
        for message_id in message_ids:
            loaded_messages.append(deepcopy(payload_by_id[message_id]))
        return loaded_messages

    async def delete_references(self, db: AsyncSession, work_id: int, kind: str) -> None:
        await db.execute(
            delete(SessionReplyCheckpointMessageReference).where(
                SessionReplyCheckpointMessageReference.work_id == work_id,
                SessionReplyCheckpointMessageReference.kind == kind,
            )
        )

    async def cleanup_unreferenced(self, db: AsyncSession, *, limit: int) -> int:
        if limit <= 0:
            return 0

        result = await db.execute(select(SessionReplyCheckpointMessage.id).where(~exists(select(1).select_from(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id))).order_by(SessionReplyCheckpointMessage.id).limit(limit).with_for_update())
        candidate_ids = [message_id for message_id in result.scalars().all() if message_id is not None]
        if not candidate_ids:
            return 0

        delete_result = await db.execute(
            delete(SessionReplyCheckpointMessage).where(
                SessionReplyCheckpointMessage.id.in_(candidate_ids),
                ~exists(select(1).select_from(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id)),
            )
        )
        return delete_result.rowcount or 0


session_reply_checkpoint_message_crud = CRUDSessionReplyCheckpointMessage()
