import json

import tiktoken
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260918_truncate_legacy_oversized_tool_results"

MESSAGE_TABLE = "message"
CHAT_SESSION_TABLE = "chat_session"
MIGRATION_BATCH_SIZE = 200
LEGACY_TOOL_RESULT_TOKEN_LIMIT = 16_000
TRUNCATION_NOTICE = "\n\n[工具响应内容过大，历史数据迁移已省略后半部分。 Historical tool response truncated during migration.]"


def _get_table_names(sync_connection) -> set[str]:
    return set(inspect(sync_connection).get_table_names())


def _canonical_json_dumps(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    )


def _truncate_content(content: str) -> str | None:
    encoding = tiktoken.get_encoding("cl100k_base")
    token_ids = encoding.encode(content, disallowed_special=())
    if len(token_ids) <= LEGACY_TOOL_RESULT_TOKEN_LIMIT:
        return None

    notice_tokens = encoding.encode(TRUNCATION_NOTICE, disallowed_special=())
    body_limit = max(1, LEGACY_TOOL_RESULT_TOKEN_LIMIT - len(notice_tokens))
    truncated_body = encoding.decode(token_ids[:body_limit])
    return f"{truncated_body}{TRUNCATION_NOTICE}"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names = await connection.run_sync(_get_table_names)
    if MESSAGE_TABLE not in table_names or CHAT_SESSION_TABLE not in table_names:
        return

    affected_sessions: set[tuple[str, str]] = set()
    last_message_id = 0

    while True:
        result = await session.execute(
            text(
                f"""
                SELECT id, session_id, uid, content
                FROM {MESSAGE_TABLE}
                WHERE id > :last_message_id
                  AND type IN (:tool_result_name, :tool_result_value)
                ORDER BY id
                LIMIT :batch_size
                """
            ),
            {
                "last_message_id": last_message_id,
                "tool_result_name": "TOOL_RESULT",
                "tool_result_value": "tool_result",
                "batch_size": MIGRATION_BATCH_SIZE,
            },
        )
        rows = result.mappings().all()
        if not rows:
            break

        for row in rows:
            last_message_id = int(row["id"])
            stored_content = row["content"]
            if not isinstance(stored_content, str):
                continue

            try:
                stored_payload = json.loads(stored_content)
            except (TypeError, ValueError):
                continue
            if not isinstance(stored_payload, dict):
                continue

            tool_content = stored_payload.get("content")
            if not isinstance(tool_content, str) or not tool_content:
                continue

            truncated_content = _truncate_content(tool_content)
            if truncated_content is None:
                continue

            stored_payload["content"] = truncated_content
            await session.execute(
                text(
                    f"""
                    UPDATE {MESSAGE_TABLE}
                    SET content = :content,
                        content_revision = content_revision + 1
                    WHERE id = :message_id
                    """
                ),
                {
                    "content": _canonical_json_dumps(stored_payload),
                    "message_id": row["id"],
                },
            )
            affected_sessions.add((str(row["session_id"]), str(row["uid"])))

    for session_id, uid in affected_sessions:
        await session.execute(
            text(
                f"""
                UPDATE {CHAT_SESSION_TABLE}
                SET context_summary = NULL,
                    context_summary_message_id = NULL,
                    context_summary_revision = context_summary_revision + 1,
                    context_content_revision = context_content_revision + 1
                WHERE session_id = :session_id
                  AND uid = :uid
                """
            ),
            {
                "session_id": session_id,
                "uid": uid,
            },
        )
