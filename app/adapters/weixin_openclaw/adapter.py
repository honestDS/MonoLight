from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, MutableSet
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.adapters.weixin_openclaw.client import WeixinOpenClawClient
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.adapters.weixin_openclaw.media import WeixinOpenClawMediaMixin
from app.adapters.weixin_openclaw.message import (
    build_session_id,
    extract_context_token,
    extract_message_timestamp,
    extract_messages,
    extract_sender_id,
    extract_text_and_attachments,
    merge_single_poll_messages,
    parse_session_user_id,
    text_item,
    update_sync_buf,
)
from app.adapters.weixin_openclaw.response import extract_event_reply, extract_reply_files, extract_reply_text
from app.adapters.weixin_openclaw.schemas import WeixinOpenClawChatResult, WeixinOpenClawMessage
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID, ERR_VALIDATION_FAILED
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.session import generate_session_title_for_active_profile

logger = get_logger(__name__)


class WeixinOpenClawAdapter(WeixinOpenClawMediaMixin, BaseChatAdapter):
    def __init__(self, config: WeixinOpenClawConfig) -> None:
        self.config = config
        self.client = WeixinOpenClawClient(config)
        self.sync_buf = config.sync_buf.strip()
        self.context_tokens: dict[str, str] = {}

    async def close(self) -> None:
        await self.client.close()

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
        return await self.client.request_json(
            method,
            endpoint,
            params=params,
            payload=payload,
            token_required=token_required,
            timeout_ms=timeout_ms,
            headers=headers,
        )

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

    async def send_session_event(self, uid: str, session_id: str, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "").strip()
        if event_type not in {"proactive_reply", "proactive_reply_error"}:
            logger.bind(uid=uid, session_id=session_id, event_type=event_type).debug(t("LOG_WEIXIN_OPENCLAW_SESSION_EVENT_IGNORED"))
            return False

        user_id = parse_session_user_id(session_id)
        if not user_id:
            logger.bind(uid=uid, session_id=session_id, event_type=event_type).warning(t("LOG_WEIXIN_OPENCLAW_SESSION_EVENT_USER_MISSING"))
            return False

        text, files = extract_event_reply(event)
        if not text and not files:
            logger.bind(uid=uid, session_id=session_id, event_type=event_type).warning(t("LOG_WEIXIN_OPENCLAW_SESSION_EVENT_EMPTY"))
            return False

        any_sent = False
        if text:
            any_sent = await self.reply_text(user_id, text)
        for file_item in files:
            any_sent = await self.reply_file_item(user_id, file_item) or any_sent

        if any_sent:
            logger.bind(uid=uid, session_id=session_id, event_type=event_type, file_count=len(files)).info(t("LOG_WEIXIN_OPENCLAW_SESSION_EVENT_SENT"))
        else:
            logger.bind(uid=uid, session_id=session_id, event_type=event_type, file_count=len(files)).warning(t("LOG_WEIXIN_OPENCLAW_SESSION_EVENT_SEND_FAILED"))
        return any_sent

    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> WeixinOpenClawChatResult:
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
                session_source="weixin-openclaw",
            )
            return WeixinOpenClawChatResult(
                text=extract_reply_text(llm_response),
                files=extract_reply_files(llm_response),
            )
        except BaseBusinessException as exc:
            return WeixinOpenClawChatResult(text=t(exc.message, default=exc.message, **exc.kwargs))
        except Exception as exc:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_WEIXIN_OPENCLAW_UNEXPECTED_ERROR", error=str(exc)), exc_info=True)
            return WeixinOpenClawChatResult(text=t(ERR_LLM_UNEXPECTED_ERROR))

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
        next_sync_buf = update_sync_buf(data)
        if next_sync_buf:
            self.sync_buf = next_sync_buf

        messages: list[WeixinOpenClawMessage] = []
        for item in extract_messages(data):
            converted = await self.convert_message(item)
            if converted is not None:
                messages.append(converted)
        if self.config.merge_single_poll_messages:
            return merge_single_poll_messages(messages)
        return messages

    async def reply_text(self, user_id: str, text: str, *, context_token: str = "") -> bool:
        return await self.reply_items(user_id, [text_item(text)], context_token=context_token)

    async def reply_items(self, user_id: str, item_list: list[dict[str, Any]], *, context_token: str = "") -> bool:
        token = context_token or self.context_tokens.get(user_id, "")
        if not token:
            logger.bind(user_id=user_id).warning(t("LOG_WEIXIN_OPENCLAW_CONTEXT_TOKEN_MISSING"))
            return False
        if not item_list:
            logger.bind(user_id=user_id).warning(t("LOG_WEIXIN_OPENCLAW_EMPTY_ITEM_LIST_IGNORED"))
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
                    "item_list": item_list,
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
        runtime_validator: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        resolved_uid = uid or message.user_id
        try:
            if runtime_validator is not None and not await runtime_validator():
                logger.bind(uid=resolved_uid, session_id=message.session_id).warning(t("LOG_WEIXIN_OPENCLAW_RUNTIME_INVALID_BEFORE_DISPATCH"))
                return False

            dispatch_text = message.text
            result = await self.chat(
                db=db,
                message=dispatch_text,
                uid=resolved_uid,
                session_id=message.session_id,
                attachments=message.attachments or None,
                active_tasks=active_tasks,
            )

            if runtime_validator is not None and not await runtime_validator():
                logger.bind(uid=resolved_uid, session_id=message.session_id).warning(t("LOG_WEIXIN_OPENCLAW_RUNTIME_INVALID_BEFORE_REPLY"))
                return False

            sent = False
            if result.text:
                sent = await self.reply_text(message.user_id, result.text, context_token=message.context_token)
            for file_item in result.files:
                sent = await self.reply_file_item(message.user_id, file_item, context_token=message.context_token) or sent

            await generate_session_title_for_active_profile(
                db=db,
                uid=resolved_uid,
                session_id=message.session_id,
                first_message=dispatch_text,
            )
            return sent
        except BaseBusinessException as exc:
            return await self._reply_error(message, exc.message, **exc.kwargs)
        except Exception as exc:
            logger.bind(uid=resolved_uid, session_id=message.session_id).exception(t("LOG_WEIXIN_OPENCLAW_MESSAGE_HANDLING_FAILED", error=str(exc)))
            return await self._reply_error(message, ERR_LLM_UNEXPECTED_ERROR)

    async def _reply_error(self, message: WeixinOpenClawMessage, error_key: str, **kwargs) -> bool:
        error_text = t(error_key, default=error_key, **kwargs)
        try:
            return await self.reply_text(message.user_id, error_text, context_token=message.context_token)
        except Exception as exc:
            logger.bind(user_id=message.user_id, session_id=message.session_id, error_key=error_key).exception(t("LOG_WEIXIN_OPENCLAW_ERROR_REPLY_FAILED", error=str(exc)))
            return False

    async def convert_message(self, message: dict[str, Any]) -> WeixinOpenClawMessage | None:
        user_id = extract_sender_id(message)
        context_token = extract_context_token(message)
        if context_token and user_id:
            self.context_tokens[user_id] = context_token

        text, attachments = await extract_text_and_attachments(self, message.get("item_list"))
        if not user_id or (not text and not attachments):
            logger.bind(user_id=user_id, has_text=bool(text), attachment_count=len(attachments), message_keys=list(message.keys())).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_MESSAGE_IGNORED"))
            return None
        return WeixinOpenClawMessage(
            user_id=user_id,
            text=text,
            session_id=build_session_id(user_id),
            context_token=context_token,
            attachments=attachments,
            raw=message,
            created_at=extract_message_timestamp(message),
        )
