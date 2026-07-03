from __future__ import annotations

import asyncio
import base64
import json
import random
import time
import uuid
from collections.abc import MutableSet
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.channel_router import select_channel
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID, ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED, ERR_VALIDATION_FAILED
from app.core.crud.profile import profile_crud
from app.core.crud.session import session_crud
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.utils.session import generate_session_title
from app.models.channel import ChannelConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"
DEFAULT_CHANNEL_VERSION = "monoligh-openclaw"
DEFAULT_API_TIMEOUT_MS = 15_000
DEFAULT_LONG_POLL_TIMEOUT_MS = 30_000
DEFAULT_POLL_INTERVAL_MS = 1_000
TEXT_ITEM_TYPE = 1


def default_weixin_openclaw_config() -> dict[str, Any]:
    return {
        "bot_type": DEFAULT_BOT_TYPE,
        "channel_version": DEFAULT_CHANNEL_VERSION,
        "api_timeout_ms": DEFAULT_API_TIMEOUT_MS,
        "long_poll_timeout_ms": DEFAULT_LONG_POLL_TIMEOUT_MS,
        "poll_interval_ms": DEFAULT_POLL_INTERVAL_MS,
    }


def normalize_weixin_openclaw_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = {**default_weixin_openclaw_config(), **dict(config or {})}
    normalized["bot_type"] = DEFAULT_BOT_TYPE
    normalized["channel_version"] = DEFAULT_CHANNEL_VERSION
    api_timeout_ms = normalized.get("api_timeout_ms")
    long_poll_timeout_ms = normalized.get("long_poll_timeout_ms")
    poll_interval_ms = normalized.get("poll_interval_ms")
    normalized["api_timeout_ms"] = max(1_000, int(DEFAULT_API_TIMEOUT_MS if api_timeout_ms in (None, "") else api_timeout_ms))
    normalized["long_poll_timeout_ms"] = max(1_000, int(DEFAULT_LONG_POLL_TIMEOUT_MS if long_poll_timeout_ms in (None, "") else long_poll_timeout_ms))
    normalized["poll_interval_ms"] = max(0, int(DEFAULT_POLL_INTERVAL_MS if poll_interval_ms in (None, "") else poll_interval_ms))
    return normalized


@dataclass
class WeixinOpenClawConfig:
    token: str = ""
    base_url: str = DEFAULT_BASE_URL
    bot_type: str = DEFAULT_BOT_TYPE
    sync_buf: str = ""
    account_id: str = ""
    channel_version: str = DEFAULT_CHANNEL_VERSION
    api_timeout_ms: int = DEFAULT_API_TIMEOUT_MS
    long_poll_timeout_ms: int = DEFAULT_LONG_POLL_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS


@dataclass
class WeixinOpenClawMessage:
    user_id: str
    text: str
    session_id: str
    context_token: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class WeixinOpenClawAdapter(BaseChatAdapter):
    def __init__(self, config: WeixinOpenClawConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.token = config.token.strip()
        self.sync_buf = config.sync_buf.strip()
        self.context_tokens: dict[str, str] = {}
        self.session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def ensure_session(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.api_timeout_ms / 1000)
            self.session = aiohttp.ClientSession(timeout=timeout)

    def build_headers(self, *, token_required: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("utf-8"),
        }
        if token_required:
            if not self.token:
                raise BaseBusinessException(message=ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED)
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        token_required: bool = True,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_session()
        if self.session is None:
            raise RuntimeError("aiohttp session is not initialized")
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=(timeout_ms or self.config.api_timeout_ms) / 1000)
        request_headers = self.build_headers(token_required=token_required)
        if headers:
            request_headers.update(headers)
        async with self.session.request(method, url, params=params, json=payload, headers=request_headers, timeout=timeout) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"{method} {endpoint} failed: {response.status} {text}")
            if not text:
                return {}
            return json.loads(text)

    async def start_login_session(self) -> dict[str, Any]:
        data = await self.request_json(
            "GET",
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": self.config.bot_type},
            token_required=False,
            timeout_ms=15_000,
        )
        qrcode = str(data.get("qrcode", "")).strip()
        qrcode_img_content = str(data.get("qrcode_img_content", "")).strip()
        if not qrcode or not qrcode_img_content:
            raise BaseBusinessException(message=ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID)
        return {
            "qrcode": qrcode,
            "qrcode_img_content": qrcode_img_content,
            "started_at": time.time(),
            "qrcode_status": "wait",
        }

    async def poll_qrcode_status(self, qrcode: str) -> dict[str, Any]:
        data = await self.request_json(
            "GET",
            "ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            token_required=False,
            timeout_ms=self.config.long_poll_timeout_ms,
            headers={"iLink-App-ClientVersion": "1"},
        )
        status = str(data.get("status", "wait")).strip()
        token = str(data.get("bot_token") or data.get("token") or "").strip()
        account_id = str(data.get("ilink_bot_id") or data.get("account_id") or data.get("user_id") or "").strip()
        base_url = str(data.get("baseurl") or data.get("base_url") or "").strip()
        user_id = str(data.get("ilink_user_id") or "").strip()
        return {
            "qrcode_status": status,
            "token": token,
            "account_id": account_id,
            "base_url": base_url,
            "user_id": user_id,
        }

    async def send_session_event(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        logger.bind(uid=uid, session_id=session_id, event_type=event.get("type")).debug("Weixin OpenClaw adapter session event ignored")

    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> str:
        if not session_id:
            raise BaseBusinessException(message=ERR_VALIDATION_FAILED, detail="session_id is required")
        try:
            llm_response = await ChatDispatcher.dispatch(
                db=db,
                message=message,
                uid=uid,
                session_id=session_id,
                attachments=attachments,
                active_tasks=active_tasks,
            )
            return self.extract_reply_text(llm_response)
        except BaseBusinessException as exc:
            return t(exc.message, default=exc.message, **exc.kwargs)
        except Exception as exc:
            logger.bind(uid=uid, session_id=session_id).error("WeixinOpenClawAdapter unexpected error: {error}", error=str(exc), exc_info=True)
            return t(ERR_LLM_UNEXPECTED_ERROR)

    async def poll_messages_once(self) -> list[WeixinOpenClawMessage]:
        data = await self.request_json(
            "POST",
            "ilink/bot/getupdates",
            payload={
                "base_info": {"channel_version": self.config.channel_version},
                "get_updates_buf": self.sync_buf,
            },
            token_required=True,
            timeout_ms=self.config.long_poll_timeout_ms,
        )
        ret = data.get("ret")
        errcode = data.get("errcode", 0)
        if ret not in (None, 0) or int(errcode or 0) != 0:
            raise RuntimeError(f"getupdates failed: ret={ret}, errcode={errcode}, errmsg={data.get('errmsg', '')}")
        self.update_sync_buf(data)
        return [message for message in (self.convert_message(item) for item in self.extract_messages(data)) if message is not None]

    async def reply_text(self, user_id: str, text: str, *, context_token: str = "") -> bool:
        token = context_token or self.context_tokens.get(user_id, "")
        if not token:
            logger.bind(user_id=user_id).warning("Weixin OpenClaw context token missing")
            return False
        await self.request_json(
            "POST",
            "ilink/bot/sendmessage",
            payload={
                "base_info": {"channel_version": self.config.channel_version},
                "msg": {
                    "from_user_id": self.config.account_id,
                    "to_user_id": user_id,
                    "client_id": uuid.uuid4().hex,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": token,
                    "item_list": [self.text_item(text)],
                },
            },
            token_required=True,
        )
        return True

    async def handle_message(
        self,
        db: AsyncSession,
        message: WeixinOpenClawMessage,
        *,
        uid: str | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> bool:
        resolved_uid = uid or message.user_id
        try:
            should_generate_title = await self.should_generate_title(db, message.session_id)
            reply = await self.chat(
                db=db,
                message=message.text,
                uid=resolved_uid,
                session_id=message.session_id,
                active_tasks=active_tasks,
            )
            if should_generate_title:
                await self.generate_title_for_message(resolved_uid, message.session_id, message.text)
            if not reply:
                return False
            return await self.reply_text(message.user_id, reply, context_token=message.context_token)
        except BaseBusinessException as exc:
            return await self._reply_error(message, exc.message, **exc.kwargs)
        except Exception as exc:
            logger.bind(uid=resolved_uid, session_id=message.session_id).exception("Weixin OpenClaw message handling failed: {error}", error=str(exc))
            return await self._reply_error(message, ERR_LLM_UNEXPECTED_ERROR)

    async def _reply_error(self, message: WeixinOpenClawMessage, error_key: str, **kwargs) -> bool:
        error_text = t(error_key, default=error_key, **kwargs)
        try:
            return await self.reply_text(message.user_id, error_text, context_token=message.context_token)
        except Exception as exc:
            logger.bind(user_id=message.user_id, session_id=message.session_id, error_key=error_key).exception("Weixin OpenClaw error reply failed: {error}", error=str(exc))
            return False

    @staticmethod
    async def should_generate_title(db: AsyncSession, session_id: str) -> bool:
        session = await session_crud.get_by_session_id(db, session_id)
        return session is None or not session.title

    @staticmethod
    async def generate_title_for_message(uid: str, session_id: str, first_message: str) -> None:
        try:
            async with AsyncSessionLocal() as title_db:
                profile = await profile_crud.get_active(title_db, uid=uid)
                if not profile:
                    return
                channel_cfg = (profile.configs or {}).get("channel", {})
                chat_channel_raw = channel_cfg.get("chat_channel")
                if not chat_channel_raw:
                    return
                chat_channel = ChannelConfig.model_validate(chat_channel_raw)
                selection = await select_channel(title_db, chat_channel, "CHAT", call_context="message_platform_title_generation", cursor_key=None)
                excluded_priorities: set[int] = set()
                while selection:
                    channel, model_entry, rule = selection
                    try:
                        await generate_session_title(
                            uid=uid,
                            session_id=session_id,
                            first_message=first_message,
                            api_key=channel.get_decrypted_api_key(),
                            base_url=channel.base_url,
                            model_id=model_entry["model_id"],
                            protocol=getattr(channel, "protocol", "openai"),
                            max_tokens=model_entry.get("max_tokens") or 200,
                            raise_on_error=True,
                        )
                        return
                    except LLMException as exc:
                        excluded_priorities.add(rule.priority)
                        logger.bind(uid=uid, session_id=session_id, **channel_log_extra(channel, model_entry)).warning(
                            "message platform title generation channel failed: {error}",
                            error=str(exc),
                        )
                    selection = await select_channel(
                        title_db,
                        chat_channel,
                        "CHAT",
                        call_context="message_platform_title_generation_retry",
                        excluded_priorities=excluded_priorities,
                        cursor_key=None,
                    )
        except Exception:
            logger.bind(uid=uid, session_id=session_id).exception("message platform title generation failed")

    def convert_message(self, message: dict[str, Any]) -> WeixinOpenClawMessage | None:
        user_id = self.extract_sender_id(message)
        text = self.extract_text(message.get("item_list"))
        context_token = self.extract_context_token(message)
        if context_token and user_id:
            self.context_tokens[user_id] = context_token
        if not user_id or not text:
            return None
        return WeixinOpenClawMessage(
            user_id=user_id,
            text=text,
            session_id=self.build_session_id(user_id),
            context_token=context_token,
            raw=message,
        )

    @staticmethod
    def build_session_id(user_id: str) -> str:
        return f"weixin-openclaw:{user_id}"

    @staticmethod
    def text_item(text: str) -> dict[str, Any]:
        return {"type": TEXT_ITEM_TYPE, "text_item": {"text": text}}

    @staticmethod
    def extract_reply_text(llm_response: dict[str, Any]) -> str:
        choices = llm_response.get("choices") if isinstance(llm_response, dict) else None
        if not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        return str(content or "").strip()

    @staticmethod
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

    @staticmethod
    def extract_context_token(message: dict[str, Any]) -> str:
        return str(message.get("context_token") or message.get("contextToken") or message.get("context") or "").strip()

    @staticmethod
    def extract_sender_id(message: dict[str, Any]) -> str:
        return str(message.get("from_user_id") or message.get("sender_user_id") or message.get("user_id") or message.get("peer_user_id") or message.get("talker") or "").strip()

    def extract_messages(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("msgs", "message_list", "msg_list", "messages", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = data.get("data")
        if isinstance(nested, dict):
            return self.extract_messages(nested)
        return []

    def update_sync_buf(self, data: dict[str, Any]) -> None:
        for key in ("get_updates_buf", "sync_buf", "syncBuf", "next_sync_buf", "nextSyncBuf"):
            value = str(data.get(key) or "").strip()
            if value:
                self.sync_buf = value
                return
        nested = data.get("data")
        if isinstance(nested, dict):
            self.update_sync_buf(nested)
