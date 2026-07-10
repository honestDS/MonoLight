import asyncio
from contextlib import asynccontextmanager
from typing import Any

from app.adapters.weixin_openclaw import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE, DEFAULT_CHANNEL_VERSION, WeixinOpenClawAdapter, WeixinOpenClawConfig, WeixinOpenClawMessage, normalize_weixin_openclaw_config
from app.adapters.weixin_openclaw.message import merge_message_pair
from app.core.crud.message_platform import message_platform_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.base import MessagePlatformHandler
from app.models.message_platform import MessagePlatform, MessagePlatformStatus, MessagePlatformType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


class WeixinOpenClawPlatformHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"weixin-openclaw"})

    async def run(self, platform_id: int) -> None:
        adapter: WeixinOpenClawAdapter | None = None
        adapter_signature: tuple | None = None
        active_message_tasks: set[asyncio.Task] = set()
        pending_messages: dict[tuple[str, str], WeixinOpenClawMessage] = {}
        pending_uids: dict[tuple[str, str], str] = {}
        pending_flush_tasks: dict[tuple[str, str], asyncio.Task] = {}
        try:
            while True:
                platform_uid = ""
                previous_sync_buf = ""
                try:
                    async with AsyncSessionLocal() as db:
                        platform = await message_platform_crud.get(db, platform_id)
                    if not self.is_pollable(platform):
                        return
                    assert platform is not None
                    platform_uid = platform.uid or ""
                    previous_sync_buf = str((platform.state or {}).get("sync_buf") or "")
                    next_signature = self._adapter_signature(platform)
                    if adapter is None or adapter_signature != next_signature:
                        if adapter is not None and active_message_tasks:
                            await asyncio.gather(*active_message_tasks, return_exceptions=True)
                            active_message_tasks.clear()
                        if adapter is not None:
                            await adapter.close()
                        adapter = self._build_adapter(platform)
                        adapter_signature = next_signature
                    else:
                        adapter.sync_buf = previous_sync_buf
                    messages = await adapter.poll_messages_once()
                    async with AsyncSessionLocal() as db:
                        platform = await message_platform_crud.get(db, platform_id)
                        if not self.is_pollable(platform):
                            return
                        assert platform is not None
                        if adapter.sync_buf != previous_sync_buf:
                            await message_platform_crud.update_runtime_state(db, platform=platform, state={"sync_buf": adapter.sync_buf}, status=MessagePlatformStatus.CONNECTED, last_error="")
                        for message in messages:
                            self._enqueue_message(
                                adapter,
                                message,
                                uid=platform_uid or message.user_id,
                                platform_id=platform_id,
                                adapter_signature=adapter_signature,
                                active_message_tasks=active_message_tasks,
                                pending_messages=pending_messages,
                                pending_uids=pending_uids,
                                pending_flush_tasks=pending_flush_tasks,
                            )
                    if adapter.config.poll_interval_ms > 0:
                        await asyncio.sleep(adapter.config.poll_interval_ms / 1000)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    continue
                except Exception as exc:
                    logger.bind(platform_id=platform_id).exception("message platform polling failed")
                    await self._mark_error(platform_id, str(exc))
                    await asyncio.sleep(5)
        finally:
            for task in active_message_tasks:
                task.cancel()
            for task in pending_flush_tasks.values():
                task.cancel()
            tasks_to_wait = list(active_message_tasks) + list(pending_flush_tasks.values())
            if tasks_to_wait:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            if adapter is not None:
                await adapter.close()

    def _enqueue_message(
        self,
        adapter: WeixinOpenClawAdapter,
        message: WeixinOpenClawMessage,
        *,
        uid: str,
        platform_id: int,
        adapter_signature: tuple | None,
        active_message_tasks: set[asyncio.Task],
        pending_messages: dict[tuple[str, str], WeixinOpenClawMessage],
        pending_uids: dict[tuple[str, str], str],
        pending_flush_tasks: dict[tuple[str, str], asyncio.Task],
    ) -> None:
        if not adapter.config.merge_single_poll_messages:
            task = asyncio.create_task(self._handle_message(adapter, message, uid=uid, platform_id=platform_id, adapter_signature=adapter_signature))
            active_message_tasks.add(task)
            task.add_done_callback(active_message_tasks.discard)
            return

        key = (message.user_id, message.session_id)
        if key in pending_messages:
            pending_messages[key] = merge_message_pair(pending_messages[key], message)
        else:
            pending_messages[key] = message
        pending_uids[key] = uid

        old_task = pending_flush_tasks.pop(key, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        delay_seconds = self._message_merge_delay_seconds(adapter)
        task = asyncio.create_task(
            self._flush_pending_message(
                adapter,
                key,
                delay_seconds,
                platform_id=platform_id,
                adapter_signature=adapter_signature,
                active_message_tasks=active_message_tasks,
                pending_messages=pending_messages,
                pending_uids=pending_uids,
                pending_flush_tasks=pending_flush_tasks,
            )
        )
        pending_flush_tasks[key] = task

    @staticmethod
    def _message_merge_delay_seconds(adapter: WeixinOpenClawAdapter) -> float:
        return max(1.5, min(5.0, adapter.config.poll_interval_ms / 1000 + 0.5))

    async def _flush_pending_message(
        self,
        adapter: WeixinOpenClawAdapter,
        key: tuple[str, str],
        delay_seconds: float,
        *,
        platform_id: int,
        adapter_signature: tuple | None,
        active_message_tasks: set[asyncio.Task],
        pending_messages: dict[tuple[str, str], WeixinOpenClawMessage],
        pending_uids: dict[tuple[str, str], str],
        pending_flush_tasks: dict[tuple[str, str], asyncio.Task],
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            message = pending_messages.pop(key, None)
            uid = pending_uids.pop(key, None)
            pending_flush_tasks.pop(key, None)
            if message is None or uid is None:
                return
            task = asyncio.create_task(self._handle_message(adapter, message, uid=uid, platform_id=platform_id, adapter_signature=adapter_signature))
            active_message_tasks.add(task)
            task.add_done_callback(active_message_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.bind(session_id=key[1]).exception("message platform pending message flush failed")

    async def _handle_message(self, adapter: WeixinOpenClawAdapter, message: WeixinOpenClawMessage, *, uid: str, platform_id: int, adapter_signature: tuple | None) -> None:
        try:
            await self._save_context_token(platform_id, message)

            async def runtime_validator() -> bool:
                try:
                    return await self._is_current_adapter(platform_id, adapter_signature)
                except Exception as exc:
                    logger.bind(uid=uid, session_id=message.session_id, platform_id=platform_id).exception(t("LOG_WEIXIN_OPENCLAW_RUNTIME_VALIDATION_FAILED", error=str(exc)))
                    return False

            async with AsyncSessionLocal() as db:
                await adapter.handle_message(db, message, uid=uid, runtime_validator=runtime_validator)
        except Exception:
            logger.bind(uid=uid, session_id=message.session_id).exception("message platform message handling failed")

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        if platform is None or platform.id is None:
            return False
        if not platform.is_enabled:
            return False
        if platform.platform_type != self.platform_type:
            return False
        if platform.status != MessagePlatformStatus.CONNECTED:
            return False
        if not platform.uid:
            return False
        return bool(platform.get_config_secret("token"))

    @staticmethod
    def _build_adapter(platform: MessagePlatform) -> WeixinOpenClawAdapter:
        config = normalize_weixin_openclaw_config(platform.config)
        state = platform.state or {}
        return WeixinOpenClawAdapter(
            WeixinOpenClawConfig(
                token=platform.get_config_secret("token"),
                base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
                cdn_base_url=str(config["cdn_base_url"]),
                bot_type=DEFAULT_BOT_TYPE,
                sync_buf=str(state.get("sync_buf") or ""),
                account_id=platform.account_id or "",
                channel_version=DEFAULT_CHANNEL_VERSION,
                api_timeout_ms=config["api_timeout_ms"],
                long_poll_timeout_ms=config["long_poll_timeout_ms"],
                poll_interval_ms=config["poll_interval_ms"],
                max_inbound_media_size_mb=config["max_inbound_media_size_mb"],
                merge_single_poll_messages=config["merge_single_poll_messages"],
            )
        )

    @staticmethod
    def _adapter_signature(platform: MessagePlatform) -> tuple:
        config = normalize_weixin_openclaw_config(platform.config)
        return (
            platform.get_config_secret("token"),
            str(config.get("base_url") or DEFAULT_BASE_URL),
            str(config["cdn_base_url"]),
            platform.account_id or "",
            config["api_timeout_ms"],
            config["long_poll_timeout_ms"],
            config["poll_interval_ms"],
            config["max_inbound_media_size_mb"],
            config["merge_single_poll_messages"],
        )

    @staticmethod
    async def _mark_error(platform_id: int, error: str) -> None:
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get(db, platform_id)
            if platform is not None:
                await message_platform_crud.update_runtime_state(db, platform=platform, status=MessagePlatformStatus.ERROR, last_error=error[:1000])

    async def _is_current_adapter(self, platform_id: int, adapter_signature: tuple | None) -> bool:
        if adapter_signature is None:
            return False
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get(db, platform_id)
            if not self.is_pollable(platform):
                return False
            try:
                assert platform is not None
                return self._adapter_signature(platform) == adapter_signature
            except Exception:
                return False

    @asynccontextmanager
    async def _build_session_adapter(self, uid: str, session_id: str, source: str):
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get_platform_for_session(db, uid=uid, session_id=session_id, source=source)
            if not self.is_pollable(platform):
                yield None
                return
            assert platform is not None
            adapter = self._build_adapter(platform)
            self._restore_context_tokens(adapter, platform)
        try:
            yield adapter
        finally:
            await adapter.close()

    @staticmethod
    def _restore_context_tokens(adapter: WeixinOpenClawAdapter, platform: MessagePlatform) -> None:
        context_tokens = (platform.state or {}).get("context_tokens")
        if not isinstance(context_tokens, dict):
            return
        adapter.context_tokens.update({str(user_id): str(token) for user_id, token in context_tokens.items() if user_id and token})

    async def _save_context_token(self, platform_id: int, message: WeixinOpenClawMessage) -> None:
        if not message.context_token:
            return
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get(db, platform_id)
            if not self.is_pollable(platform):
                return
            assert platform is not None
            state = dict(platform.state or {})
            context_tokens = state.get("context_tokens") if isinstance(state.get("context_tokens"), dict) else {}
            context_tokens = dict(context_tokens)
            if context_tokens.get(message.user_id) == message.context_token:
                return
            context_tokens[message.user_id] = message.context_token
            await message_platform_crud.update_runtime_state(db, platform=platform, state={"context_tokens": context_tokens})

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        if source not in self.sources:
            return False
        async with self._build_session_adapter(uid, session_id, source) as adapter:
            if adapter is None:
                return False
            return await adapter.send_session_event(uid, session_id, event)


weixin_openclaw_platform_handler = WeixinOpenClawPlatformHandler()
