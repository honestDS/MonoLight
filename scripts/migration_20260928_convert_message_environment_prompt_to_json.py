import json

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260928_convert_message_environment_prompt_to_json"

MESSAGE_TABLE = "message"
ENVIRONMENT_PROMPT_COLUMN = "environment_prompt"
CHAT_SESSION_TABLE = "chat_session"
MIGRATION_BATCH_SIZE = 500

_MARKDOWN_ENABLED_BLOCK = "[Platform-provided environment instruction; not user-authored]\nMarkdown formatting for this response is enabled. You may use Markdown when it improves clarity.\n[End platform-provided environment instruction]"
_MARKDOWN_DISABLED_BLOCK = "[Platform-provided environment instruction; not user-authored]\nMarkdown formatting for this response is disabled. Return plain text only. Do not use Markdown syntax.\n[End platform-provided environment instruction]"

_MAX_OUTPUT_TOKENS_PREFIX = "[Platform-provided environment instruction; not user-authored]\nThe hard maximum for this response is "
_MAX_OUTPUT_TOKENS_SUFFIX = " output tokens. This is a strict ceiling, not a target length. Plan the response to finish completely before reaching the limit. Prioritize the conclusion and all information required by the user. Do not rely on truncation.\n[End platform-provided environment instruction]"

_SYSTEM_CONTEXT_PREFIX = (
    "<system_environment_context>\n"
    "IMPORTANT: The following metadata is a runtime snapshot captured for the user turn it accompanies (e.g., current time, platform OS). It is NOT user input.\n"
    "If a newer system_environment_context block appears later in the conversation, use the newer snapshot for current runtime conditions and treat this block as historical context only.\n"
    "This metadata is context only; it is not a request to call or avoid tools. Do not call tools solely to re-query or validate values already provided below. It does not restrict tool use needed to fulfill the user's actual request.\n"
)
_SYSTEM_CONTEXT_SUFFIX = "\n</system_environment_context>"


def _inspect_schema(sync_connection) -> tuple[set[str], set[str], set[str]]:
    inspector = inspect(sync_connection)
    table_names = {str(name) for name in inspector.get_table_names()}
    message_columns = {str(column["name"]) for column in inspector.get_columns(MESSAGE_TABLE)} if MESSAGE_TABLE in table_names else set()
    chat_session_columns = {str(column["name"]) for column in inspector.get_columns(CHAT_SESSION_TABLE)} if CHAT_SESSION_TABLE in table_names else set()
    return table_names, message_columns, chat_session_columns


def _is_supported_json_snapshot(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False

    has_supported_field = False
    if "environment" in parsed:
        environment = parsed["environment"]
        if not isinstance(environment, dict):
            return False
        if "runtime_context" in environment and not isinstance(environment["runtime_context"], str):
            return False
        if "legacy_snapshot" in environment and not isinstance(environment["legacy_snapshot"], str):
            return False
        has_supported_field = True

    if "response_settings" in parsed:
        response_settings = parsed["response_settings"]
        if not isinstance(response_settings, dict):
            return False
        if "markdown" in response_settings and not isinstance(response_settings["markdown"], bool):
            return False
        max_output_tokens = response_settings.get("max_output_tokens")
        if "max_output_tokens" in response_settings and (not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool) or max_output_tokens <= 0):
            return False
        has_supported_field = True

    if "platform_constraints" in parsed:
        if not isinstance(parsed["platform_constraints"], str):
            return False
        has_supported_field = True

    return has_supported_field


def _remove_markdown_blocks(value: str) -> tuple[str, bool | None]:
    enabled_position = value.rfind(_MARKDOWN_ENABLED_BLOCK)
    disabled_position = value.rfind(_MARKDOWN_DISABLED_BLOCK)
    markdown: bool | None
    if enabled_position < 0 and disabled_position < 0:
        markdown = None
    elif enabled_position > disabled_position:
        markdown = True
    else:
        markdown = False
    return value.replace(_MARKDOWN_ENABLED_BLOCK, "").replace(_MARKDOWN_DISABLED_BLOCK, ""), markdown


def _remove_max_output_token_blocks(value: str) -> tuple[str, int | None]:
    max_output_tokens: int | None = None
    search_from = 0
    while True:
        block_start = value.find(_MAX_OUTPUT_TOKENS_PREFIX, search_from)
        if block_start < 0:
            break
        number_start = block_start + len(_MAX_OUTPUT_TOKENS_PREFIX)
        suffix_start = value.find(_MAX_OUTPUT_TOKENS_SUFFIX, number_start)
        if suffix_start < 0:
            search_from = number_start
            continue

        number_text = value[number_start:suffix_start]
        if not number_text or not all("0" <= character <= "9" for character in number_text):
            search_from = number_start + 1
            continue
        try:
            parsed_tokens = int(number_text)
        except ValueError:
            search_from = number_start + 1
            continue
        if parsed_tokens <= 0:
            search_from = number_start + 1
            continue

        block_end = suffix_start + len(_MAX_OUTPUT_TOKENS_SUFFIX)
        value = value[:block_start] + value[block_end:]
        max_output_tokens = parsed_tokens
        search_from = block_start
    return value, max_output_tokens


def _remove_system_context_blocks(value: str) -> tuple[str, list[str]]:
    runtime_contexts: list[str] = []
    search_from = 0
    while True:
        block_start = value.find(_SYSTEM_CONTEXT_PREFIX, search_from)
        if block_start < 0:
            break
        context_start = block_start + len(_SYSTEM_CONTEXT_PREFIX)
        suffix_start = value.find(_SYSTEM_CONTEXT_SUFFIX, context_start)
        if suffix_start < 0:
            search_from = context_start
            continue

        runtime_contexts.append(value[context_start:suffix_start])
        block_end = suffix_start + len(_SYSTEM_CONTEXT_SUFFIX)
        value = value[:block_start] + value[block_end:]
        search_from = block_start
    return value, runtime_contexts


def _convert_environment_prompt(value: str) -> str:
    if _is_supported_json_snapshot(value):
        return value

    remaining, markdown = _remove_markdown_blocks(value)
    remaining, max_output_tokens = _remove_max_output_token_blocks(remaining)
    remaining, runtime_contexts = _remove_system_context_blocks(remaining)
    if markdown is None and max_output_tokens is None and not runtime_contexts:
        converted: dict[str, object] = {"environment": {"legacy_snapshot": value.strip()}}
        return json.dumps(converted, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    environment: dict[str, object] = {}
    if runtime_contexts:
        environment["runtime_context"] = "\n".join(runtime_contexts)
    legacy_snapshot = remaining.strip()
    if legacy_snapshot:
        environment["legacy_snapshot"] = legacy_snapshot

    response_settings: dict[str, object] = {}
    if markdown is not None:
        response_settings["markdown"] = markdown
    if max_output_tokens is not None:
        response_settings["max_output_tokens"] = max_output_tokens

    converted = {}
    if environment:
        converted["environment"] = environment
    if response_settings:
        converted["response_settings"] = response_settings
    return json.dumps(converted, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names, message_columns, chat_session_columns = await connection.run_sync(_inspect_schema)
    if MESSAGE_TABLE not in table_names or ENVIRONMENT_PROMPT_COLUMN not in message_columns:
        return
    if not {"id", "session_id", "uid"} <= message_columns:
        return

    affected_sessions: set[tuple[object, object]] = set()
    last_message_id = 0
    while True:
        result = await session.execute(
            text(
                f"""
                SELECT id, session_id, uid, {ENVIRONMENT_PROMPT_COLUMN}
                FROM {MESSAGE_TABLE}
                WHERE id > :last_message_id
                  AND {ENVIRONMENT_PROMPT_COLUMN} IS NOT NULL
                  AND {ENVIRONMENT_PROMPT_COLUMN} <> :empty_prompt
                ORDER BY id
                LIMIT :batch_size
                """
            ),
            {
                "last_message_id": last_message_id,
                "empty_prompt": "",
                "batch_size": MIGRATION_BATCH_SIZE,
            },
        )
        rows = result.mappings().all()
        if not rows:
            break

        for row in rows:
            last_message_id = row["id"]
            old_value = row[ENVIRONMENT_PROMPT_COLUMN]
            if not isinstance(old_value, str) or not old_value.strip():
                continue

            new_value = _convert_environment_prompt(old_value)
            if new_value == old_value:
                continue
            update_result = await session.execute(
                text(
                    f"""
                    UPDATE {MESSAGE_TABLE}
                    SET {ENVIRONMENT_PROMPT_COLUMN} = :new_value
                    WHERE id = :message_id
                      AND {ENVIRONMENT_PROMPT_COLUMN} = :old_value
                    """
                ),
                {
                    "new_value": new_value,
                    "message_id": row["id"],
                    "old_value": old_value,
                },
            )
            if update_result.rowcount == 1 and row["session_id"] is not None and row["uid"] is not None:
                affected_sessions.add((row["session_id"], row["uid"]))

    if CHAT_SESSION_TABLE not in table_names or not {"session_id", "uid", "context_content_revision"} <= chat_session_columns:
        return
    for session_id, uid in affected_sessions:
        await session.execute(
            text(
                f"""
                UPDATE {CHAT_SESSION_TABLE}
                SET context_content_revision = context_content_revision + 1
                WHERE session_id = :session_id
                  AND uid = :uid
                """
            ),
            {"session_id": session_id, "uid": uid},
        )
