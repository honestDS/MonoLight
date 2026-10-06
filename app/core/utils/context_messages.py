import json

from pydantic import ValidationError

from app.models.message import (
    InternalMessage,
    MessagePart,
    MessageRole,
    TextPart,
)


def to_jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value


def is_context_summary_message(message: InternalMessage) -> bool:
    return isinstance(message.content, str) and message.content.startswith("<conversation_summary ")


def merge_user_message_contents(
    contents: list[str | list[MessagePart] | None],
) -> str | list[MessagePart]:
    normalized: list[str | list[MessagePart] | None] = []
    for content in contents:
        if not isinstance(content, str):
            normalized.append(content)
            continue

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            normalized.append(content)
            continue
        if not isinstance(parsed, list) or not parsed:
            normalized.append(content)
            continue

        try:
            parsed_content = InternalMessage(
                role=MessageRole.USER,
                content=parsed,
            ).content
        except ValidationError:
            normalized.append(content)
            continue
        if not isinstance(parsed_content, list) or any(type(part) is MessagePart for part in parsed_content):
            normalized.append(content)
            continue
        normalized.append(parsed_content)

    if not any(isinstance(content, list) for content in normalized):
        return "\n".join(content or "" for content in normalized)

    merged: list[MessagePart] = []
    for content in normalized:
        if isinstance(content, list):
            merged.extend(content)
            continue
        if not content:
            continue
        merged.append(TextPart(text=content))

    return merged


def message_token_text(msg: InternalMessage) -> str:
    if msg.tool_calls:
        return msg.model_dump_json(exclude_none=True)
    if isinstance(msg.content, str):
        return msg.content
    if msg.content is None:
        return ""
    if isinstance(msg.content, list):
        text_parts: list[str] = []
        for part in msg.content:
            part_type = getattr(part, "type", "")
            if part_type == "text":
                text_parts.append(str(getattr(part, "text", "") or ""))
            elif part_type == "image_url":
                text_parts.append("[图片]")
            elif part_type == "audio":
                text_parts.append("[音频]")
            elif part_type == "file":
                text_parts.append(f"[文件:{getattr(part, 'path', '') or ''}]")
            else:
                text_parts.append(json.dumps(to_jsonable(part), ensure_ascii=False))
        return "\n".join(item for item in text_parts if item)
    return json.dumps(to_jsonable(msg.content), ensure_ascii=False)
