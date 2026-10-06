import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.utils.system import get_full_system_context
from app.models.message import InternalMessage, MessageRole, TextPart

_SNAPSHOT_FIELDS = ("environment", "response_settings", "platform_constraints")


def _serialize_environment_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def _normalize_platform_constraint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_json_environment_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}

    raw_environment = snapshot.get("environment")
    if isinstance(raw_environment, dict):
        environment: dict[str, Any] = {}
        runtime_context = raw_environment.get("runtime_context")
        if isinstance(runtime_context, str):
            environment["runtime_context"] = runtime_context
        legacy_snapshot = raw_environment.get("legacy_snapshot")
        if isinstance(legacy_snapshot, str) and legacy_snapshot.strip():
            environment["legacy_snapshot"] = legacy_snapshot.strip()
        if environment:
            normalized["environment"] = environment

    raw_response_settings = snapshot.get("response_settings")
    if isinstance(raw_response_settings, dict):
        response_settings: dict[str, Any] = {}
        markdown = raw_response_settings.get("markdown")
        if isinstance(markdown, bool):
            response_settings["markdown"] = markdown
        max_output_tokens = raw_response_settings.get("max_output_tokens")
        if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and max_output_tokens > 0:
            response_settings["max_output_tokens"] = max_output_tokens
        if response_settings:
            normalized["response_settings"] = response_settings

    platform_constraints = _normalize_platform_constraint(snapshot.get("platform_constraints"))
    if platform_constraints is not None:
        normalized["platform_constraints"] = platform_constraints

    return normalized


def _normalize_environment_prompt(environment_prompt: str | None) -> dict[str, Any]:
    if not isinstance(environment_prompt, str) or not environment_prompt.strip():
        return {}
    try:
        parsed = json.loads(environment_prompt)
    except json.JSONDecodeError:
        return {"environment": {"legacy_snapshot": environment_prompt}}
    if not isinstance(parsed, dict) or not any(field in parsed for field in _SNAPSHOT_FIELDS):
        return {"environment": {"legacy_snapshot": environment_prompt}}
    return _normalize_json_environment_snapshot(parsed)


def materialize_user_environment_prompts(messages: list[InternalMessage]) -> list[InternalMessage]:
    request_messages = [message.model_copy(deep=True) for message in messages]
    last_user_index = max((index for index, message in enumerate(request_messages) if message.role == MessageRole.USER), default=-1)
    latest_platform_constraints: str | None = None

    for index, message in enumerate(request_messages):
        if message.role != MessageRole.USER:
            continue

        normalized_environment = _normalize_environment_prompt(message.environment_prompt)
        own_platform_constraints = normalized_environment.get("platform_constraints")
        if isinstance(own_platform_constraints, str) and own_platform_constraints:
            latest_platform_constraints = own_platform_constraints

        if isinstance(message.content, list):
            user_message: Any = []
            real_non_text_parts: list[Any] = []
            attachment_index = 0
            for part in message.content:
                part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                if isinstance(part, TextPart) or part_type == "text":
                    text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                    if not isinstance(text, str):
                        text = "" if text is None else str(text)
                    user_message.append({"type": "text", "text": text})
                    continue
                media_type = part_type if isinstance(part_type, str) and part_type else "unknown"
                user_message.append({"type": "attachment", "index": attachment_index, "media_type": media_type})
                real_non_text_parts.append(part)
                attachment_index += 1
        else:
            user_message = message.content
            real_non_text_parts = []

        payload: dict[str, Any] = {"user_message": user_message}
        if message.attachments:
            payload["attachment_paths"] = list(message.attachments)
        for field in ("environment", "response_settings", "platform_constraints"):
            if field in normalized_environment:
                payload[field] = normalized_environment[field]
        if "platform_constraints" not in normalized_environment and latest_platform_constraints is not None:
            payload["platform_constraints"] = latest_platform_constraints

        if index == last_user_index:
            guidance_prompt = message.guidance_prompt.strip() if isinstance(message.guidance_prompt, str) else ""
            if guidance_prompt:
                payload["platform_guidance"] = guidance_prompt

        materialized_content = _serialize_environment_snapshot(payload)
        if real_non_text_parts:
            message.content = [TextPart(text=materialized_content), *real_non_text_parts]
        else:
            message.content = materialized_content
    return request_messages


async def build_user_runtime_instructions(
    db: AsyncSession,
    session_id: str,
    max_tokens: int = 0,
    *,
    platform_constraints: str | None = None,
) -> str:
    session = await session_crud.get_by_session_id(db, session_id)
    snapshot: dict[str, Any] = {
        "environment": {"runtime_context": get_full_system_context()},
        "response_settings": {"markdown": bool(session.enable_markdown) if session else False},
    }
    if max_tokens > 0:
        snapshot["response_settings"]["max_output_tokens"] = max_tokens
    normalized_platform_constraints = _normalize_platform_constraint(platform_constraints)
    if normalized_platform_constraints is not None:
        snapshot["platform_constraints"] = normalized_platform_constraints
    return _serialize_environment_snapshot(snapshot)


def append_user_runtime_instruction_text(message: InternalMessage, instruction: str) -> InternalMessage:
    message.environment_prompt = instruction
    return message


def apply_platform_constraints_to_message(message: InternalMessage, platform_constraints: str | None) -> InternalMessage:
    normalized_platform_constraints = _normalize_platform_constraint(platform_constraints)
    if normalized_platform_constraints is None:
        return message
    snapshot = _normalize_environment_prompt(message.environment_prompt)
    snapshot["platform_constraints"] = normalized_platform_constraints
    message.environment_prompt = _serialize_environment_snapshot(snapshot)
    return message


async def ensure_user_runtime_instructions(
    db: AsyncSession,
    session_id: str,
    message: InternalMessage,
    max_tokens: int = 0,
    *,
    instruction: str | None = None,
    platform_constraints: str | None = None,
) -> InternalMessage:
    if isinstance(message.environment_prompt, str) and message.environment_prompt:
        previous_environment_prompt = message.environment_prompt
        if _normalize_platform_constraint(platform_constraints) is not None:
            apply_platform_constraints_to_message(message, platform_constraints)
            if message.environment_prompt != previous_environment_prompt and message.id is not None:
                await message_crud.set_environment_prompt(db, message.id, message.environment_prompt)
        return message

    if instruction is None:
        instruction = await build_user_runtime_instructions(
            db,
            session_id,
            max_tokens,
            platform_constraints=platform_constraints,
        )
    else:
        snapshot = _normalize_environment_prompt(instruction)
        normalized_platform_constraints = _normalize_platform_constraint(platform_constraints)
        if normalized_platform_constraints is not None:
            snapshot["platform_constraints"] = normalized_platform_constraints
        instruction = _serialize_environment_snapshot(snapshot)
    previous_environment_prompt = message.environment_prompt
    append_user_runtime_instruction_text(message, instruction)
    if message.id is not None and message.environment_prompt != previous_environment_prompt:
        await message_crud.set_environment_prompt(db, message.id, instruction)
    return message


def refresh_max_output_tokens_instruction(message: InternalMessage, max_tokens: int) -> InternalMessage:
    snapshot = _normalize_environment_prompt(message.environment_prompt)
    response_settings = snapshot.get("response_settings")
    if not isinstance(response_settings, dict):
        response_settings = {}
        snapshot["response_settings"] = response_settings
    if max_tokens > 0:
        response_settings["max_output_tokens"] = max_tokens
    else:
        response_settings.pop("max_output_tokens", None)
    if not response_settings:
        snapshot.pop("response_settings", None)
    message.environment_prompt = _serialize_environment_snapshot(snapshot)
    return message


async def refresh_latest_user_max_output_tokens_instruction(
    db: AsyncSession,
    messages: list[InternalMessage],
    max_tokens: int,
) -> None:
    for message in reversed(messages):
        if message.role != MessageRole.USER:
            continue
        refresh_max_output_tokens_instruction(message, max_tokens)
        if message.id is not None:
            await message_crud.set_environment_prompt(db, message.id, message.environment_prompt)
        return
