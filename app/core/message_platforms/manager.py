import asyncio
import time
import uuid
from contextlib import suppress

from app.adapters.weixin_openclaw import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE, DEFAULT_CHANNEL_VERSION, WeixinOpenClawAdapter, WeixinOpenClawConfig, WeixinOpenClawMessage, normalize_weixin_openclaw_config
from app.core.crud.active_session import active_session_crud
from app.core.crud.message_platform import message_platform_crud
from app.core.log import get_logger
from app.models.message_platform import MessagePlatform, MessagePlatformStatus, MessagePlatformType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

POLL_LEASE_GRACE_SECONDS = 30
POLL_LEASE_MIN_SECONDS = 60


class MessagePlatformPollingManager:
    def __init__(self) -> None:
        self._refresh_task: asyncio.Task | None = None
        self._tasks: dict[int, asyncio.Task] = {}
        self._stopping = False
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._refresh_task is None or self._refresh_task.done():
            self._stopping = False
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            tasks.append(self._refresh_task)
            self._refresh_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def reload(self) -> None:
        async with self._lock:
            await self._sync_enabled_tasks()

    async def restart_platform(self, platform_id: int) -> None:
        task = self._tasks.pop(platform_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.reload()

    async def _refresh_loop(self) -> None:
        while not self._stopping:
            try:
                await self.reload()
            except Exception:
                logger.exception("message platform task refresh failed")
            await asyncio.sleep(10)

    async def _sync_enabled_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            platforms = await message_platform_crud.list_pollable(db)
        enabled_ids = {platform.id for platform in platforms if platform.id is not None}
        for platform_id, task in list(self._tasks.items()):
            if platform_id not in enabled_ids or task.done():
                self._tasks.pop(platform_id, None)
                if not task.done():
                    task.cancel()
        for platform in platforms:
            if platform.id is None or platform.id in self._tasks:
                continue
            self._tasks[platform.id] = asyncio.create_task(self._run_weixin_openclaw(platform.id))

    async def _run_weixin_openclaw(self, platform_id: int) -> None:
        adapter: WeixinOpenClawAdapter | None = None
        adapter_signature: tuple | None = None
        active_message_tasks: set[asyncio.Task] = set()
        poll_lock_key = f"message-platform:{platform_id}"
        poll_owner = uuid.uuid4().hex
        try:
            while not self._stopping:
                platform_uid = ""
                previous_sync_buf = ""
                try:
                    should_continue, platform, previous_sync_buf = await self._claim_poll(platform_id, poll_lock_key, poll_owner)
                    if not should_continue:
                        return
                    if platform is None:
                        await asyncio.sleep(1)
                        continue
                    platform_uid = platform.uid or ""
                    next_signature = self._weixin_adapter_signature(platform)
                    if adapter is None or adapter_signature != next_signature:
                        if adapter is not None and active_message_tasks:
                            await asyncio.gather(*active_message_tasks, return_exceptions=True)
                            active_message_tasks.clear()
                        if adapter is not None:
                            await adapter.close()
                        adapter = self._build_weixin_adapter(platform)
                        adapter_signature = next_signature
                    else:
                        adapter.sync_buf = previous_sync_buf
                    messages = await adapter.poll_messages_once()
                    async with AsyncSessionLocal() as db:
                        platform = await message_platform_crud.get(db, platform_id)
                        if not self._is_pollable(platform):
                            return
                        assert platform is not None
                        if not self._poll_owner_matches(platform, poll_owner):
                            continue
                        if adapter.sync_buf != previous_sync_buf:
                            await message_platform_crud.update_runtime_state(db, platform=platform, state={"sync_buf": adapter.sync_buf}, status=MessagePlatformStatus.CONNECTED, last_error="")
                        await self._release_poll_lease(db, platform)
                        for message in messages:
                            task = asyncio.create_task(self._handle_weixin_message(adapter, message, uid=platform_uid or message.user_id))
                            active_message_tasks.add(task)
                            task.add_done_callback(active_message_tasks.discard)
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
                    await self._release_poll_lease_if_owned(platform_id, poll_owner)
        finally:
            for task in active_message_tasks:
                task.cancel()
            if active_message_tasks:
                await asyncio.gather(*active_message_tasks, return_exceptions=True)
            if adapter is not None:
                await adapter.close()

    async def _claim_poll(self, platform_id: int, poll_lock_key: str, poll_owner: str) -> tuple[bool, MessagePlatform | None, str]:
        async with AsyncSessionLocal() as db:
            await active_session_crud.cleanup_expired_locks(db)
            lock_acquired = await active_session_crud.acquire_lock(db, poll_lock_key)
            if not lock_acquired:
                return True, None, ""
            try:
                platform = await message_platform_crud.get(db, platform_id)
                if not self._is_pollable(platform):
                    return False, None, ""
                assert platform is not None
                state = dict(platform.state or {})
                lease = state.get("poll_lease") if isinstance(state.get("poll_lease"), dict) else {}
                lease_until = float(lease.get("until") or 0)
                if lease_until > time.time() and lease.get("owner") != poll_owner:
                    return True, None, ""
                previous_sync_buf = str(state.get("sync_buf") or "")
                lease_seconds = self._poll_lease_seconds(platform)
                await message_platform_crud.update_runtime_state(
                    db,
                    platform=platform,
                    state={"poll_lease": {"owner": poll_owner, "until": time.time() + lease_seconds}},
                )
                return True, platform, previous_sync_buf
            finally:
                await active_session_crud.release_lock(db, poll_lock_key)

    @staticmethod
    def _poll_lease_seconds(platform: MessagePlatform) -> float:
        config = normalize_weixin_openclaw_config(platform.config)
        return max(POLL_LEASE_MIN_SECONDS, config["long_poll_timeout_ms"] / 1000 + POLL_LEASE_GRACE_SECONDS)

    @staticmethod
    def _poll_owner_matches(platform: MessagePlatform, poll_owner: str) -> bool:
        lease = (platform.state or {}).get("poll_lease")
        return isinstance(lease, dict) and lease.get("owner") == poll_owner

    async def _release_poll_lease_if_owned(self, platform_id: int, poll_owner: str) -> None:
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get(db, platform_id)
            if platform is not None and self._poll_owner_matches(platform, poll_owner):
                await self._release_poll_lease(db, platform)

    @staticmethod
    async def _release_poll_lease(db, platform: MessagePlatform) -> None:
        await message_platform_crud.update_runtime_state(db, platform=platform, state={"poll_lease": {}})

    async def _handle_weixin_message(self, adapter: WeixinOpenClawAdapter, message: WeixinOpenClawMessage, *, uid: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await adapter.handle_message(db, message, uid=uid)
        except Exception:
            logger.bind(uid=uid, session_id=message.session_id).exception("message platform message handling failed")

    def _is_pollable(self, platform: MessagePlatform | None) -> bool:
        if platform is None or platform.id is None:
            return False
        if not platform.is_enabled:
            return False
        if platform.platform_type != MessagePlatformType.WEIXIN_OPENCLAW:
            return False
        if platform.status != MessagePlatformStatus.CONNECTED:
            return False
        if not platform.uid:
            return False
        return bool(platform.get_config_secret("token"))

    def _build_weixin_adapter(self, platform: MessagePlatform) -> WeixinOpenClawAdapter:
        config = normalize_weixin_openclaw_config(platform.config)
        state = platform.state or {}
        return WeixinOpenClawAdapter(
            WeixinOpenClawConfig(
                token=platform.get_config_secret("token"),
                base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
                bot_type=DEFAULT_BOT_TYPE,
                sync_buf=str(state.get("sync_buf") or ""),
                account_id=platform.account_id or "",
                channel_version=DEFAULT_CHANNEL_VERSION,
                api_timeout_ms=config["api_timeout_ms"],
                long_poll_timeout_ms=config["long_poll_timeout_ms"],
                poll_interval_ms=config["poll_interval_ms"],
            )
        )

    def _weixin_adapter_signature(self, platform: MessagePlatform) -> tuple:
        config = normalize_weixin_openclaw_config(platform.config)
        return (
            platform.get_config_secret("token"),
            str(config.get("base_url") or DEFAULT_BASE_URL),
            platform.account_id or "",
            config["api_timeout_ms"],
            config["long_poll_timeout_ms"],
            config["poll_interval_ms"],
        )

    async def _mark_error(self, platform_id: int, error: str) -> None:
        async with AsyncSessionLocal() as db:
            platform = await message_platform_crud.get(db, platform_id)
            if platform is not None:
                await message_platform_crud.update_runtime_state(db, platform=platform, status=MessagePlatformStatus.ERROR, last_error=error[:1000])


message_platform_polling_manager = MessagePlatformPollingManager()
