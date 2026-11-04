import hashlib
import json
from typing import Any, NoReturn

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261104_02_normalize_session_reply_checkpoint_messages"

_WORK_BATCH_SIZE = 25
_MESSAGE_BATCH_SIZE = 200

_METADATA = MetaData()

_CHAT_SESSION = Table(
    "chat_session",
    _METADATA,
    Column("session_id", String(100), primary_key=True),
    Column("uid", String(100), nullable=False),
    UniqueConstraint("session_id", "uid", name="uq_chat_session_session_uid"),
)

_WORK_ITEM = Table(
    "session_reply_work_item",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("uid", String(100), nullable=False),
    Column("session_id", String(100), nullable=False),
    Column("execution_state", JSON),
)

_WORK_CHECKPOINT = Table(
    "session_reply_work_checkpoint",
    _METADATA,
    Column(
        "work_id",
        Integer,
        ForeignKey("session_reply_work_item.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("checkpoint", JSON, nullable=False),
)

_CHECKPOINT_MESSAGE = Table(
    "session_reply_checkpoint_message",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("uid", String(100), nullable=False),
    Column("session_id", String(100), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("session_id", "uid", "content_hash"),
    ForeignKeyConstraint(
        ["session_id", "uid"],
        ["chat_session.session_id", "chat_session.uid"],
        ondelete="CASCADE",
    ),
)

_MESSAGE_REFERENCE = Table(
    "session_reply_checkpoint_message_reference",
    _METADATA,
    Column(
        "work_id",
        Integer,
        ForeignKey("session_reply_work_item.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("kind", String(20), primary_key=True),
    Column(
        "message_id",
        Integer,
        ForeignKey("session_reply_checkpoint_message.id", ondelete="CASCADE"),
        index=True,
        primary_key=True,
    ),
)


def _raise_migration_error(work_id: int, detail: str) -> NoReturn:
    raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: {detail}")


def _canonical_payload(payload: Any, work_id: int, detail: str) -> tuple[str, str]:
    if not isinstance(payload, dict):
        _raise_migration_error(work_id, f"{detail} is not a message object")
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: {detail} is not valid JSON") from exc
    return canonical, digest


def _classify_message_list(value: Any, work_id: int, field: str) -> tuple[str, list[Any]]:
    if not isinstance(value, list):
        _raise_migration_error(work_id, f"{field} must be a list")
    if not value:
        return "empty", []

    if all(isinstance(item, dict) for item in value):
        return "payloads", value
    if all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value):
        return "ids", value
    _raise_migration_error(work_id, f"{field} contains mixed or invalid message values")


def _classify_frozen_message_list(value: Any, work_id: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise_migration_error(work_id, "frozen_context_messages must be a list")
    if not all(isinstance(item, dict) for item in value):
        _raise_migration_error(work_id, "frozen_context_messages must contain only message objects")
    return value


def _classify_frozen_ids(value: Any, work_id: int) -> list[int]:
    if not isinstance(value, list):
        _raise_migration_error(work_id, "frozen_context_message_ids must be a list")
    if not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value):
        _raise_migration_error(work_id, "frozen_context_message_ids must contain only positive integer ids")
    return value


def _add_payload(
    payload: dict[str, Any],
    *,
    work_id: int,
    detail: str,
    payload_by_hash: dict[str, dict[str, Any]],
    canonical_by_hash: dict[str, str],
    hash_order: list[str],
) -> str:
    canonical, content_hash = _canonical_payload(payload, work_id, detail)
    previous_canonical = canonical_by_hash.get(content_hash)
    if previous_canonical is not None and previous_canonical != canonical:
        _raise_migration_error(work_id, f"same content hash has different message bodies ({detail})")
    if previous_canonical is None:
        canonical_by_hash[content_hash] = canonical
        payload_by_hash[content_hash] = dict(payload)
        hash_order.append(content_hash)
    return content_hash


async def _load_message_rows_by_ids(
    session: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    message_ids: list[int],
) -> dict[int, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(message_ids))
    rows_by_id: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(unique_ids), _MESSAGE_BATCH_SIZE):
        id_batch = unique_ids[offset : offset + _MESSAGE_BATCH_SIZE]
        result = await session.execute(
            select(
                _CHECKPOINT_MESSAGE.c.id,
                _CHECKPOINT_MESSAGE.c.uid,
                _CHECKPOINT_MESSAGE.c.session_id,
                _CHECKPOINT_MESSAGE.c.content_hash,
                _CHECKPOINT_MESSAGE.c.payload,
            ).where(_CHECKPOINT_MESSAGE.c.id.in_(id_batch))
        )
        for row in result.mappings().all():
            message_id = row["id"]
            if message_id is not None:
                rows_by_id[message_id] = dict(row)

    for message_id in unique_ids:
        row = rows_by_id.get(message_id)
        if row is None:
            _raise_migration_error(work_id, f"message id {message_id} does not exist")
        if row["uid"] != uid or row["session_id"] != session_id:
            _raise_migration_error(work_id, f"message id {message_id} belongs to another session scope")
        canonical, actual_hash = _canonical_payload(
            row["payload"],
            work_id,
            f"stored message id {message_id} payload",
        )
        if row["content_hash"] != actual_hash:
            _raise_migration_error(work_id, f"stored message id {message_id} has an inconsistent content hash")
        row["canonical"] = canonical
    return rows_by_id


async def _load_message_rows_by_hashes(
    session: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    hashes: list[str],
    canonical_by_hash: dict[str, str],
) -> dict[str, dict[str, Any]]:
    rows_by_hash: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(hashes), _MESSAGE_BATCH_SIZE):
        hash_batch = hashes[offset : offset + _MESSAGE_BATCH_SIZE]
        result = await session.execute(
            select(
                _CHECKPOINT_MESSAGE.c.id,
                _CHECKPOINT_MESSAGE.c.uid,
                _CHECKPOINT_MESSAGE.c.session_id,
                _CHECKPOINT_MESSAGE.c.content_hash,
                _CHECKPOINT_MESSAGE.c.payload,
            ).where(
                _CHECKPOINT_MESSAGE.c.uid == uid,
                _CHECKPOINT_MESSAGE.c.session_id == session_id,
                _CHECKPOINT_MESSAGE.c.content_hash.in_(hash_batch),
            )
        )
        for row in result.mappings().all():
            row_data = dict(row)
            content_hash = row_data["content_hash"]
            if content_hash in rows_by_hash:
                _raise_migration_error(work_id, f"duplicate stored message rows for content hash {content_hash}")
            canonical, actual_hash = _canonical_payload(
                row_data["payload"],
                work_id,
                f"stored message hash {content_hash} payload",
            )
            if actual_hash != content_hash:
                _raise_migration_error(work_id, f"stored message hash {content_hash} does not match its payload")
            expected_canonical = canonical_by_hash.get(content_hash)
            if expected_canonical is not None and canonical != expected_canonical:
                _raise_migration_error(work_id, f"same content hash has different stored message body ({content_hash})")
            row_data["canonical"] = canonical
            rows_by_hash[content_hash] = row_data
    return rows_by_hash


async def _materialize_messages(
    session: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    hash_order: list[str],
    payload_by_hash: dict[str, dict[str, Any]],
    canonical_by_hash: dict[str, str],
) -> dict[str, int]:
    if not hash_order:
        return {}

    session_result = await session.execute(
        select(_CHAT_SESSION.c.session_id).where(
            _CHAT_SESSION.c.session_id == session_id,
            _CHAT_SESSION.c.uid == uid,
        )
    )
    if session_result.scalar_one_or_none() is None:
        _raise_migration_error(work_id, "work item has no matching chat session owner")

    existing_rows = await _load_message_rows_by_hashes(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        hashes=hash_order,
        canonical_by_hash=canonical_by_hash,
    )
    missing_hashes = [content_hash for content_hash in hash_order if content_hash not in existing_rows]
    for offset in range(0, len(missing_hashes), _MESSAGE_BATCH_SIZE):
        hash_batch = missing_hashes[offset : offset + _MESSAGE_BATCH_SIZE]
        await session.execute(
            insert(_CHECKPOINT_MESSAGE).values(
                [
                    {
                        "uid": uid,
                        "session_id": session_id,
                        "content_hash": content_hash,
                        "payload": payload_by_hash[content_hash],
                    }
                    for content_hash in hash_batch
                ]
            )
        )

    stored_rows = await _load_message_rows_by_hashes(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        hashes=hash_order,
        canonical_by_hash=canonical_by_hash,
    )
    message_id_by_hash: dict[str, int] = {}
    for content_hash in hash_order:
        row = stored_rows.get(content_hash)
        if row is None or row["id"] is None:
            _raise_migration_error(work_id, f"message body for content hash {content_hash} was not stored")
        message_id_by_hash[content_hash] = row["id"]
    return message_id_by_hash


async def _verify_payload_groups(
    session: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    groups: list[tuple[str, list[str], list[int]]],
    canonical_by_hash: dict[str, str],
) -> None:
    all_ids = [message_id for _, _, message_ids in groups for message_id in message_ids]
    rows_by_id = await _load_message_rows_by_ids(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        message_ids=all_ids,
    )
    for label, expected_hashes, message_ids in groups:
        if len(expected_hashes) != len(message_ids):
            _raise_migration_error(work_id, f"stored message order or length differs for {label}")
        for index, (content_hash, message_id) in enumerate(zip(expected_hashes, message_ids, strict=True)):
            row = rows_by_id.get(message_id)
            if row is None or row["canonical"] != canonical_by_hash[content_hash]:
                _raise_migration_error(work_id, f"stored message at {label}[{index}] differs from the old body")


async def _sync_references(
    session: AsyncSession,
    *,
    work_id: int,
    kind: str,
    desired_ids: set[int],
) -> None:
    result = await session.execute(
        select(_MESSAGE_REFERENCE.c.message_id).where(
            _MESSAGE_REFERENCE.c.work_id == work_id,
            _MESSAGE_REFERENCE.c.kind == kind,
        )
    )
    existing_ids = set(result.scalars().all())
    ids_to_remove = existing_ids - desired_ids
    if ids_to_remove:
        await session.execute(
            delete(_MESSAGE_REFERENCE).where(
                _MESSAGE_REFERENCE.c.work_id == work_id,
                _MESSAGE_REFERENCE.c.kind == kind,
                _MESSAGE_REFERENCE.c.message_id.in_(ids_to_remove),
            )
        )

    ids_to_add = desired_ids - existing_ids
    if ids_to_add:
        await session.execute(
            insert(_MESSAGE_REFERENCE).values(
                [
                    {
                        "work_id": work_id,
                        "kind": kind,
                        "message_id": message_id,
                    }
                    for message_id in sorted(ids_to_add)
                ]
            )
        )


async def _migrate_work(
    session: AsyncSession,
    *,
    work_id: int,
    uid: str,
    session_id: str,
    state: Any,
    checkpoint_exists: bool,
    checkpoint: Any,
) -> None:
    if state is not None and not isinstance(state, dict):
        _raise_migration_error(work_id, "execution_state is not an object")
    if isinstance(state, dict) and "dispatcher_checkpoint" in state:
        _raise_migration_error(
            work_id,
            "execution_state still contains dispatcher_checkpoint; migration 20261104_01_split_session_reply_checkpoint is missing",
        )

    if checkpoint_exists and not isinstance(checkpoint, dict):
        _raise_migration_error(work_id, "checkpoint is not an object")

    payload_by_hash: dict[str, dict[str, Any]] = {}
    canonical_by_hash: dict[str, str] = {}
    hash_order: list[str] = []
    checkpoint_hashes_by_key: dict[str, list[str]] = {}
    checkpoint_ids_by_key: dict[str, list[int]] = {}
    checkpoint_groups_to_verify: list[tuple[str, list[str]]] = []
    id_groups_to_validate: list[list[int]] = []

    if checkpoint_exists:
        for key in ("messages", "turn_messages"):
            if key not in checkpoint:
                continue
            value_kind, values = _classify_message_list(checkpoint[key], work_id, f"checkpoint.{key}")
            if value_kind == "payloads":
                hashes = [
                    _add_payload(
                        payload,
                        work_id=work_id,
                        detail=f"checkpoint.{key}[{index}]",
                        payload_by_hash=payload_by_hash,
                        canonical_by_hash=canonical_by_hash,
                        hash_order=hash_order,
                    )
                    for index, payload in enumerate(values)
                ]
                checkpoint_hashes_by_key[key] = hashes
                checkpoint_groups_to_verify.append((f"checkpoint.{key}", hashes))
            else:
                ids = [int(message_id) for message_id in values]
                checkpoint_ids_by_key[key] = ids
                id_groups_to_validate.append(ids)

    state_updated = dict(state) if isinstance(state, dict) else None
    has_old_frozen = isinstance(state, dict) and "frozen_context_messages" in state
    has_new_frozen = isinstance(state, dict) and "frozen_context_message_ids" in state
    old_frozen_payloads: list[dict[str, Any]] | None = None
    old_frozen_canonicals: list[str] | None = None
    old_frozen_hashes: list[str] | None = None
    new_frozen_ids: list[int] | None = None

    if has_old_frozen:
        old_frozen_payloads = _classify_frozen_message_list(state["frozen_context_messages"], work_id)
        old_frozen_canonicals = [_canonical_payload(payload, work_id, f"frozen_context_messages[{index}]")[0] for index, payload in enumerate(old_frozen_payloads)]
    if has_new_frozen:
        new_frozen_ids = _classify_frozen_ids(state["frozen_context_message_ids"], work_id)
        id_groups_to_validate.append(new_frozen_ids)

    all_ids_to_validate = [message_id for group in id_groups_to_validate for message_id in group]
    id_rows = await _load_message_rows_by_ids(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        message_ids=all_ids_to_validate,
    )

    frozen_ids: list[int]
    if has_old_frozen and has_new_frozen:
        if old_frozen_canonicals is None or new_frozen_ids is None:
            _raise_migration_error(work_id, "frozen context representation is incomplete")
        if len(old_frozen_canonicals) != len(new_frozen_ids):
            _raise_migration_error(work_id, "frozen_context_messages and frozen_context_message_ids differ in length")
        for index, (expected_canonical, message_id) in enumerate(zip(old_frozen_canonicals, new_frozen_ids, strict=True)):
            row = id_rows.get(message_id)
            if row is None or row["canonical"] != expected_canonical:
                _raise_migration_error(work_id, f"frozen context message at index {index} differs from the old body")
        frozen_ids = list(new_frozen_ids)
    elif has_new_frozen:
        if new_frozen_ids is None:
            _raise_migration_error(work_id, "frozen_context_message_ids representation is incomplete")
        frozen_ids = list(new_frozen_ids)
    elif has_old_frozen:
        if old_frozen_payloads is None:
            _raise_migration_error(work_id, "frozen_context_messages representation is incomplete")
        old_frozen_hashes = [
            _add_payload(
                payload,
                work_id=work_id,
                detail=f"frozen_context_messages[{index}]",
                payload_by_hash=payload_by_hash,
                canonical_by_hash=canonical_by_hash,
                hash_order=hash_order,
            )
            for index, payload in enumerate(old_frozen_payloads)
        ]
        checkpoint_groups_to_verify.append(("frozen_context_messages", old_frozen_hashes))
        frozen_ids = []
    else:
        frozen_ids = []

    message_id_by_hash = await _materialize_messages(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        hash_order=hash_order,
        payload_by_hash=payload_by_hash,
        canonical_by_hash=canonical_by_hash,
    )

    verification_groups: list[tuple[str, list[str], list[int]]] = []
    for label, hashes in checkpoint_groups_to_verify:
        message_ids = [message_id_by_hash[content_hash] for content_hash in hashes]
        verification_groups.append((label, hashes, message_ids))
        if label == "frozen_context_messages":
            frozen_ids = list(message_ids)
    await _verify_payload_groups(
        session,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        groups=verification_groups,
        canonical_by_hash=canonical_by_hash,
    )

    if has_old_frozen:
        assert state_updated is not None
        state_updated["frozen_context_message_ids"] = list(frozen_ids)
        del state_updated["frozen_context_messages"]

    desired_checkpoint_ids: set[int] = set()
    checkpoint_updated: dict[str, Any] | None = None
    if checkpoint_exists:
        checkpoint_updated = dict(checkpoint)
        for key, hashes in checkpoint_hashes_by_key.items():
            message_ids = [message_id_by_hash[content_hash] for content_hash in hashes]
            checkpoint_updated[key] = message_ids
            desired_checkpoint_ids.update(message_ids)
        for ids in checkpoint_ids_by_key.values():
            desired_checkpoint_ids.update(ids)

    if checkpoint_exists and checkpoint_updated != checkpoint:
        await session.execute(update(_WORK_CHECKPOINT).where(_WORK_CHECKPOINT.c.work_id == work_id).values(checkpoint=checkpoint_updated))
    if state is not None and state_updated != state:
        await session.execute(update(_WORK_ITEM).where(_WORK_ITEM.c.id == work_id).values(execution_state=state_updated))

    await _sync_references(
        session,
        work_id=work_id,
        kind="checkpoint",
        desired_ids=desired_checkpoint_ids,
    )
    await _sync_references(
        session,
        work_id=work_id,
        kind="frozen",
        desired_ids=set(frozen_ids),
    )


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: _CHECKPOINT_MESSAGE.create(sync_connection, checkfirst=True))
    await connection.run_sync(lambda sync_connection: _MESSAGE_REFERENCE.create(sync_connection, checkfirst=True))

    orphan_result = await session.execute(
        select(_WORK_CHECKPOINT.c.work_id)
        .select_from(
            _WORK_CHECKPOINT.outerjoin(
                _WORK_ITEM,
                _WORK_ITEM.c.id == _WORK_CHECKPOINT.c.work_id,
            )
        )
        .where(_WORK_ITEM.c.id.is_(None))
        .order_by(_WORK_CHECKPOINT.c.work_id)
        .limit(1)
    )
    orphan_work_id = orphan_result.scalar()
    if orphan_work_id is not None:
        _raise_migration_error(orphan_work_id, "checkpoint exists without a work item")

    last_id: int | None = None
    while True:
        statement = (
            select(
                _WORK_ITEM.c.id.label("work_id"),
                _WORK_ITEM.c.uid.label("uid"),
                _WORK_ITEM.c.session_id.label("session_id"),
                _WORK_ITEM.c.execution_state.label("execution_state"),
                _WORK_CHECKPOINT.c.work_id.label("checkpoint_work_id"),
                _WORK_CHECKPOINT.c.checkpoint.label("checkpoint"),
            )
            .select_from(
                _WORK_ITEM.outerjoin(
                    _WORK_CHECKPOINT,
                    _WORK_CHECKPOINT.c.work_id == _WORK_ITEM.c.id,
                )
            )
            .order_by(_WORK_ITEM.c.id.asc())
            .limit(_WORK_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(_WORK_ITEM.c.id > last_id)

        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return

        for row in rows:
            work_id = row["work_id"]
            if work_id is None:
                continue
            await _migrate_work(
                session,
                work_id=work_id,
                uid=row["uid"],
                session_id=row["session_id"],
                state=row["execution_state"],
                checkpoint_exists=row["checkpoint_work_id"] is not None,
                checkpoint=row["checkpoint"],
            )

        last_id = rows[-1]["work_id"]
