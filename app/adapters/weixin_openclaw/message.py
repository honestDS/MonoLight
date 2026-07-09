from __future__ import annotations

from pathlib import Path
from typing import Any

from app.adapters.weixin_openclaw.constants import (
    FILE_ITEM_TYPE,
    IMAGE_ITEM_TYPE,
    SKIPPED_MEDIA_PLACEHOLDERS,
    TEXT_ITEM_TYPE,
    VIDEO_ITEM_TYPE,
    VOICE_ITEM_TYPE,
)
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.core.i18n import t
from app.core.log import get_logger

logger = get_logger(__name__)


WEIXIN_OPENCLAW_SESSION_PREFIX = "weixin-openclaw:"


def build_session_id(user_id: str) -> str:
    return f"{WEIXIN_OPENCLAW_SESSION_PREFIX}{user_id}"


def parse_session_user_id(session_id: str) -> str:
    if not session_id.startswith(WEIXIN_OPENCLAW_SESSION_PREFIX):
        return ""
    return session_id[len(WEIXIN_OPENCLAW_SESSION_PREFIX) :].strip()


def text_item(text: str) -> dict[str, Any]:
    return {"type": TEXT_ITEM_TYPE, "text_item": {"text": text}}


def extract_text(item_list: list[dict[str, Any]] | None) -> str:
    if not item_list:
        return ""
    texts: list[str] = []
    for item in item_list:
        item_type = int(item.get("type") or 0)
        if item_type == TEXT_ITEM_TYPE:
            text = str(item.get("text_item", {}).get("text", "")).strip()
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def extract_context_token(message: dict[str, Any]) -> str:
    return str(message.get("context_token") or message.get("contextToken") or message.get("context") or "").strip()


def extract_sender_id(message: dict[str, Any]) -> str:
    return str(message.get("from_user_id") or message.get("sender_user_id") or message.get("user_id") or message.get("peer_user_id") or message.get("talker") or "").strip()


def extract_message_timestamp(message: dict[str, Any]) -> float:
    value = message.get("create_time_ms") or message.get("createTimeMs") or message.get("create_time") or message.get("createTime") or message.get("timestamp")
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1_000_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return 0.0
        return parsed / 1000 if parsed > 1_000_000_000_000 else parsed
    return 0.0


def extract_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("msgs", "message_list", "msg_list", "messages", "list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get("data")
    if isinstance(nested, dict):
        return extract_messages(nested)
    return []


def update_sync_buf(data: dict[str, Any]) -> str:
    for key in ("get_updates_buf", "sync_buf", "syncBuf", "next_sync_buf", "nextSyncBuf"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        return update_sync_buf(nested)
    return ""


def merge_single_poll_messages(messages: list[WeixinOpenClawMessage]) -> list[WeixinOpenClawMessage]:
    grouped: dict[tuple[str, str], WeixinOpenClawMessage] = {}
    ordered_keys: list[tuple[str, str]] = []
    for message in messages:
        key = (message.user_id, message.session_id)
        if key not in grouped:
            grouped[key] = message
            ordered_keys.append(key)
            continue
        grouped[key] = merge_message_pair(grouped[key], message)
    return [grouped[key] for key in ordered_keys]


def merge_message_pair(left: WeixinOpenClawMessage, right: WeixinOpenClawMessage) -> WeixinOpenClawMessage:
    text = "\n".join(part for part in (left.text, right.text) if part).strip()
    raw_messages: list[dict[str, Any]] = []
    if isinstance(left.raw, dict) and isinstance(left.raw.get("messages"), list):
        raw_messages.extend(item for item in left.raw["messages"] if isinstance(item, dict))
    elif left.raw:
        raw_messages.append(left.raw)
    if right.raw:
        raw_messages.append(right.raw)

    return WeixinOpenClawMessage(
        user_id=left.user_id,
        text=text,
        session_id=left.session_id,
        context_token=right.context_token or left.context_token,
        attachments=[*left.attachments, *right.attachments],
        raw={"type": "merged_split_messages", "messages": raw_messages},
        created_at=min(value for value in (left.created_at, right.created_at) if value > 0) if left.created_at > 0 or right.created_at > 0 else 0.0,
    )


def build_attachment_fallback_text(attachments: list[str]) -> str:
    if not attachments:
        return ""
    return "\n".join(f"[文件:{Path(item).name}]" for item in attachments)


async def extract_text_and_attachments(adapter: Any, item_list: list[dict[str, Any]] | None) -> tuple[str, list[str]]:
    if not item_list:
        return "", []

    texts: list[str] = []
    attachments: list[str] = []
    for item in item_list:
        item_type = int(item.get("type") or 0)
        if item_type == TEXT_ITEM_TYPE:
            text = str(item.get("text_item", {}).get("text", "")).strip()
            if text:
                texts.append(text)
            continue

        if item_type == IMAGE_ITEM_TYPE:
            media_path = await adapter.resolve_inbound_image(item)
            if media_path:
                attachments.append(str(media_path))
            continue

        if item_type == FILE_ITEM_TYPE:
            media_path = await adapter.resolve_inbound_file(item)
            if media_path:
                texts.append(f"[文件:{media_path.name}]")
                attachments.append(str(media_path))
            continue

        if item_type == VOICE_ITEM_TYPE:
            voice_text = str(item.get("voice_item", {}).get("text", "")).strip()
            texts.append(voice_text or SKIPPED_MEDIA_PLACEHOLDERS[VOICE_ITEM_TYPE])
            continue

        if item_type == VIDEO_ITEM_TYPE:
            texts.append(SKIPPED_MEDIA_PLACEHOLDERS[VIDEO_ITEM_TYPE])
            continue

        ref = item.get("ref_msg")
        if isinstance(ref, dict):
            ref_item = ref.get("message_item")
            if isinstance(ref_item, dict):
                ref_text, _ref_attachments = await extract_text_and_attachments(adapter, [ref_item])
                if ref_text:
                    texts.append(f"[引用:{ref_text}]")
            continue

        logger.bind(item_type=item_type, item_keys=list(item.keys())).warning(t("LOG_WEIXIN_OPENCLAW_UNSUPPORTED_ITEM_IGNORED"))

    return "\n".join(texts).strip(), attachments
