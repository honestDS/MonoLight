import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

from sqlalchemy import MetaData, Table, delete, func, inspect, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.dispatcher.checkpoint_message import checkpoint_message_hash
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261107_01_add_subagent_names"

_BATCH_SIZE = 200
_DEFAULT_NAME = "历史子任务"


async def _iter_rows(session: AsyncSession, statement: Any, primary_key: Any) -> AsyncIterator[Any]:
    cursor = None
    primary_keys = primary_key if isinstance(primary_key, tuple) else (primary_key,)
    while True:
        page_statement = statement.order_by(*[column.asc() for column in primary_keys]).limit(_BATCH_SIZE)
        if cursor is not None:
            if len(primary_keys) == 1:
                page_statement = page_statement.where(primary_keys[0] > cursor)
            else:
                page_statement = page_statement.where(tuple_(*primary_keys) > cursor)
        rows = (await session.execute(page_statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            yield row
        if len(primary_keys) == 1:
            cursor = rows[-1][primary_keys[0].key]
        else:
            cursor = tuple(rows[-1][column.key] for column in primary_keys)


def _name_from_message(message: Any) -> str:
    if not isinstance(message, str):
        return _DEFAULT_NAME
    normalized = " ".join(message.split())
    normalized = "".join(character for character in normalized if ord(character) >= 32 and not 127 <= ord(character) <= 159 and character not in "\u2028\u2029")
    return normalized[:32] or _DEFAULT_NAME


def _has_valid_message(message: Any) -> bool:
    if not isinstance(message, str):
        return False
    normalized = " ".join(message.split())
    normalized = "".join(character for character in normalized if ord(character) >= 32 and not 127 <= ord(character) <= 159 and character not in "\u2028\u2029")
    return bool(normalized)


def _valid_name(name: Any) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 32 and bool(name.strip()) and all(ord(character) >= 32 and not 127 <= ord(character) <= 159 and character not in "\u2028\u2029" for character in name)


def _transform_call(
    call: Any,
    *,
    uid: str,
    session_id: str,
    names_by_call: dict[tuple[str, str, str], str],
    detail: str,
) -> bool:
    if not isinstance(call, dict):
        return False
    if call.get("name") != "agent_work":
        return False
    arguments = call.get("arguments")
    if not isinstance(arguments, dict) or arguments.get("operation") != "create":
        return False

    call_id = call.get("id", call.get("tool_call_id"))
    mapped_name = names_by_call.get((uid, session_id, call_id)) if isinstance(call_id, str) else None
    if "name" in arguments:
        if not _valid_name(arguments["name"]):
            raise RuntimeError(f"{MIGRATION_ID}: {detail} has an invalid name")
        if mapped_name is not None and arguments["name"] != mapped_name:
            raise RuntimeError(f"{MIGRATION_ID}: {detail} name conflicts with its subagent")
        return False

    message = arguments.get("message")
    if mapped_name is None and not _has_valid_message(message):
        return False
    arguments["name"] = mapped_name or _name_from_message(message)
    call.pop("provider_metadata", None)
    return True


def _transform_message(
    message: Any,
    *,
    uid: str,
    session_id: str,
    names_by_call: dict[tuple[str, str, str], str],
    detail: str,
    message_type: str | None = None,
) -> bool:
    if not isinstance(message, dict):
        return False
    changed = False
    if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
        for index, call in enumerate(message["tool_calls"]):
            if _transform_call(
                call,
                uid=uid,
                session_id=session_id,
                names_by_call=names_by_call,
                detail=f"{detail}.tool_calls[{index}]",
            ):
                changed = True

    effective_type = message_type or message.get("type")
    if effective_type == "subagent_result" and isinstance(message.get("content"), str):
        try:
            result = json.loads(message["content"])
        except (TypeError, ValueError):
            result = None
        if isinstance(result, dict) and result.get("type") == "external_work_resume" and result.get("source") == "subagent":
            call = result.get("tool_call")
            if _transform_call(
                call,
                uid=uid,
                session_id=session_id,
                names_by_call=names_by_call,
                detail=f"{detail}.content.tool_call",
            ):
                message["content"] = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                changed = True

    if changed:
        message.pop("provider_metadata", None)
    return changed


def _transform_structure(
    value: Any,
    *,
    uid: str,
    session_id: str,
    names_by_call: dict[tuple[str, str, str], str],
    id_redirects: dict[int, int],
    detail: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    changed = False

    if value.get("source") == "subagent" and _transform_call(
        value.get("tool_call"),
        uid=uid,
        session_id=session_id,
        names_by_call=names_by_call,
        detail=f"{detail}.tool_call",
    ):
        changed = True

    for key in ("messages", "turn_messages", "history", "external_resume_messages"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, int) and not isinstance(item, bool) and item in id_redirects:
                items[index] = id_redirects[item]
                changed = True
            elif _transform_message(
                item,
                uid=uid,
                session_id=session_id,
                names_by_call=names_by_call,
                detail=f"{detail}.{key}[{index}]",
            ):
                changed = True

    for key in ("external_resume_context", "checkpoint_metadata", "metadata", "response", "checkpoint"):
        nested = value.get(key)
        if isinstance(nested, dict):
            if _transform_structure(
                nested,
                uid=uid,
                session_id=session_id,
                names_by_call=names_by_call,
                id_redirects=id_redirects,
                detail=f"{detail}.{key}",
            ):
                changed = True
        elif key == "external_resume_context" and isinstance(nested, list):
            for index, item in enumerate(nested):
                if isinstance(item, dict) and _transform_structure(
                    item,
                    uid=uid,
                    session_id=session_id,
                    names_by_call=names_by_call,
                    id_redirects=id_redirects,
                    detail=f"{detail}.{key}[{index}]",
                ):
                    changed = True

    frozen_ids = value.get("frozen_context_message_ids")
    if isinstance(frozen_ids, list):
        for index, message_id in enumerate(frozen_ids):
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id in id_redirects:
                frozen_ids[index] = id_redirects[message_id]
                changed = True
    return changed


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    connection = await session.connection()

    def reflect(sync_connection: Any) -> dict[str, Table]:
        existing = set(inspect(sync_connection).get_table_names())
        if "subagent" in existing:
            columns = {column["name"] for column in inspect(sync_connection).get_columns("subagent")}
            if "name" not in columns:
                sync_connection.exec_driver_sql("ALTER TABLE subagent ADD COLUMN name VARCHAR(32) NOT NULL DEFAULT ''")
        metadata = MetaData()
        names = (
            "subagent",
            "subagent_tool_call",
            "session_reply_work_item",
            "message",
            "chat_session",
            "session_reply_checkpoint_message",
            "session_reply_work_checkpoint",
            "session_reply_checkpoint_message_reference",
        )
        tables = {}
        for name in names:
            if name in existing:
                tables[name] = Table(name, metadata, autoload_with=sync_connection)
        return tables

    tables = await connection.run_sync(reflect)
    subagents = tables.get("subagent")
    calls = tables.get("subagent_tool_call")
    works = tables.get("session_reply_work_item")
    messages = tables.get("message")
    chat_sessions = tables.get("chat_session")
    checkpoint_messages = tables.get("session_reply_checkpoint_message")
    work_checkpoints = tables.get("session_reply_work_checkpoint")
    references = tables.get("session_reply_checkpoint_message_reference")

    earliest: dict[str, dict[str, Any]] = {}
    if calls is not None:
        statement = select(calls.c.id, calls.c.subagent_id, calls.c.arguments)
        async for row in _iter_rows(session, statement, calls.c.id):
            arguments = row["arguments"]
            if isinstance(arguments, dict) and arguments.get("operation") == "create":
                earliest.setdefault(row["subagent_id"], arguments)

    names_by_subagent: dict[str, str] = {}
    if subagents is not None:
        statement = select(subagents.c.id, subagents.c.name)
        async for row in _iter_rows(session, statement, subagents.c.id):
            current_name = row["name"]
            if current_name:
                if not _valid_name(current_name):
                    raise RuntimeError(f"{MIGRATION_ID}: subagent {row['id']} has an invalid name")
                name = current_name
            else:
                old_call = earliest.get(row["id"], {})
                call_name = old_call.get("name")
                if call_name is not None and not _valid_name(call_name):
                    raise RuntimeError(f"{MIGRATION_ID}: saved create call has an invalid name")
                name = call_name or _name_from_message(old_call.get("message"))
                await session.execute(update(subagents).where(subagents.c.id == row["id"]).values(name=name))
            names_by_subagent[row["id"]] = name

    names_by_call: dict[tuple[str, str, str], str] = {}
    if calls is not None and works is not None:
        statement = select(calls.c.id, calls.c.subagent_id, calls.c.tool_call_id, calls.c.arguments, works.c.uid, works.c.session_id).select_from(calls.join(works, calls.c.parent_work_id == works.c.id))
        async for row in _iter_rows(session, statement, calls.c.id):
            arguments = row["arguments"]
            if not isinstance(arguments, dict) or arguments.get("operation") != "create":
                continue
            name = names_by_subagent.get(row["subagent_id"])
            if name is None:
                continue
            if "name" in arguments:
                if not _valid_name(arguments["name"]) or arguments["name"] != name:
                    raise RuntimeError(f"{MIGRATION_ID}: saved create call name conflicts with its subagent")
            else:
                arguments["name"] = name
                await session.execute(update(calls).where(calls.c.id == row["id"]).values(arguments=arguments))
            key = (row["uid"], row["session_id"], row["tool_call_id"])
            if key in names_by_call and names_by_call[key] != name:
                raise RuntimeError(f"{MIGRATION_ID}: tool call maps to conflicting subagent names")
            names_by_call[key] = name

    if messages is not None:
        statement = select(messages).where(messages.c.type.in_(("TOOL_CALL", "SUBAGENT_RESULT")))
        async for row in _iter_rows(session, statement, messages.c.id):
            if row["type"] == "SUBAGENT_RESULT":
                payload = {"role": "user", "type": "subagent_result", "content": row["content"]}
            else:
                if row["role"] != "ASSISTANT":
                    continue
                try:
                    payload = json.loads(row["content"] or "{}")
                except (TypeError, ValueError):
                    continue
            if not _transform_message(
                payload,
                uid=row["uid"],
                session_id=row["session_id"],
                names_by_call=names_by_call,
                detail=f"message {row['id']}",
                message_type="subagent_result" if row["type"] == "SUBAGENT_RESULT" else "tool_call",
            ):
                continue
            values: dict[str, Any] = {
                "content": (payload["content"] if row["type"] == "SUBAGENT_RESULT" else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
                "provider_metadata": None,
                "content_revision": func.coalesce(messages.c.content_revision, 0) + 1,
            }
            await session.execute(update(messages).where(messages.c.id == row["id"]).values(**values))
            if chat_sessions is not None:
                await session.execute(update(chat_sessions).where(chat_sessions.c.uid == row["uid"], chat_sessions.c.session_id == row["session_id"]).values(context_content_revision=func.coalesce(chat_sessions.c.context_content_revision, 0) + 1))

    id_redirects: dict[int, int] = {}
    if checkpoint_messages is not None:
        statement = select(checkpoint_messages)
        async for row in _iter_rows(session, statement, checkpoint_messages.c.id):
            payload = row["payload"]
            old_payload = deepcopy(payload)
            try:
                old_hash = checkpoint_message_hash(old_payload)
            except (TypeError, ValueError, OverflowError, UnicodeError):
                old_hash = None
            if not _transform_message(
                payload,
                uid=row["uid"],
                session_id=row["session_id"],
                names_by_call=names_by_call,
                detail=f"checkpoint message {row['id']}",
            ):
                continue
            if old_hash is None or old_hash != row["content_hash"]:
                raise RuntimeError(f"{MIGRATION_ID}: checkpoint message {row['id']} has an inconsistent content hash")
            content_hash = checkpoint_message_hash(payload)
            target = (
                (
                    await session.execute(
                        select(checkpoint_messages.c.id, checkpoint_messages.c.payload).where(
                            checkpoint_messages.c.uid == row["uid"],
                            checkpoint_messages.c.session_id == row["session_id"],
                            checkpoint_messages.c.content_hash == content_hash,
                            checkpoint_messages.c.id != row["id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                await session.execute(update(checkpoint_messages).where(checkpoint_messages.c.id == row["id"]).values(payload=payload, content_hash=content_hash))
                continue
            expected = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            actual = json.dumps(target["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if actual != expected:
                raise RuntimeError(f"{MIGRATION_ID}: checkpoint hash collision has different payloads")
            id_redirects[row["id"]] = target["id"]

    if works is not None:
        statement = select(works.c.id, works.c.uid, works.c.session_id, works.c.execution_state)
        async for row in _iter_rows(session, statement, works.c.id):
            state = row["execution_state"]
            if _transform_structure(
                state,
                uid=row["uid"],
                session_id=row["session_id"],
                names_by_call=names_by_call,
                id_redirects=id_redirects,
                detail=f"work {row['id']}.execution_state",
            ):
                await session.execute(update(works).where(works.c.id == row["id"]).values(execution_state=state))

    if work_checkpoints is not None and works is not None:
        statement = select(work_checkpoints.c.work_id, work_checkpoints.c.checkpoint, works.c.uid, works.c.session_id).select_from(work_checkpoints.join(works, work_checkpoints.c.work_id == works.c.id))
        async for row in _iter_rows(session, statement, work_checkpoints.c.work_id):
            checkpoint = row["checkpoint"]
            if _transform_structure(
                checkpoint,
                uid=row["uid"],
                session_id=row["session_id"],
                names_by_call=names_by_call,
                id_redirects=id_redirects,
                detail=f"work {row['work_id']}.checkpoint",
            ):
                await session.execute(update(work_checkpoints).where(work_checkpoints.c.work_id == row["work_id"]).values(checkpoint=checkpoint))

    if references is not None and checkpoint_messages is not None:
        for old_id, target_id in id_redirects.items():
            statement = select(references.c.work_id, references.c.kind, references.c.message_id).where(references.c.message_id == old_id)
            async for row in _iter_rows(
                session,
                statement,
                (references.c.work_id, references.c.kind, references.c.message_id),
            ):
                existing = await session.execute(
                    select(references.c.message_id).where(
                        references.c.work_id == row["work_id"],
                        references.c.kind == row["kind"],
                        references.c.message_id == target_id,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    await session.execute(
                        update(references)
                        .where(
                            references.c.work_id == row["work_id"],
                            references.c.kind == row["kind"],
                            references.c.message_id == old_id,
                        )
                        .values(message_id=target_id)
                    )
                else:
                    await session.execute(
                        delete(references).where(
                            references.c.work_id == row["work_id"],
                            references.c.kind == row["kind"],
                            references.c.message_id == old_id,
                        )
                    )
        for old_id in id_redirects:
            await session.execute(delete(checkpoint_messages).where(checkpoint_messages.c.id == old_id))
