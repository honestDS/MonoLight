import asyncio
from typing import Any

import pytest

import app.models.message_platform as message_platform_model
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.models.message_platform import MessagePlatform, MessagePlatformResponse, MessagePlatformType


class StubMessagePlatformHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"stub-source"})

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return platform is not None

    async def run(self, platform_id: int) -> None:
        return None

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        return source in self.sources


def test_manager_registers_handler_by_platform_type():
    handler = StubMessagePlatformHandler()
    manager = MessagePlatformPollingManager((handler,))

    assert manager.get_handler(MessagePlatformType.WEIXIN_OPENCLAW) is handler


@pytest.mark.asyncio
async def test_manager_routes_session_event_by_source():
    manager = MessagePlatformPollingManager((StubMessagePlatformHandler(),))

    assert await manager.send_session_event("uid", "session", "stub-source", {"type": "completed"}) is True
    assert await manager.send_session_event("uid", "session", "unknown-source", {"type": "completed"}) is False


def test_manager_accepts_empty_handler_registry():
    manager = MessagePlatformPollingManager(())

    assert manager.get_handler(MessagePlatformType.WEIXIN_OPENCLAW) is None


def test_message_platform_response_decrypts_config_secrets(monkeypatch):
    monkeypatch.setattr(
        message_platform_model,
        "decrypt_api_key",
        lambda value: {"encrypted-token": "plain-token", "encrypted-bot-token": "plain-bot-token"}[value],
    )
    platform = MessagePlatform(
        id=1,
        name="platform",
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        config={
            "token": "enc:v1:encrypted-token",
            "bot_token": "enc:v1:encrypted-bot-token",
            "base_url": "https://example.com",
        },
    )

    response = MessagePlatformResponse.model_validate(platform)

    assert response.config == {
        "token": "plain-token",
        "bot_token": "plain-bot-token",
        "base_url": "https://example.com",
    }


@pytest.mark.parametrize(
    ("config", "decrypt_api_key", "expected"),
    [
        (
            {"token": "plain-token", "bot_token": "plain-bot-token", "base_url": "https://example.com"},
            lambda value: pytest.fail(f"unexpected decryption: {value}"),
            {"token": "plain-token", "bot_token": "plain-bot-token", "base_url": "https://example.com"},
        ),
        (
            {"token": "enc:v1:bad-token", "bot_token": "enc:v1:bad-bot-token"},
            lambda value: (_ for _ in ()).throw(ValueError(value)),
            {"token": "", "bot_token": ""},
        ),
    ],
)
def test_message_platform_response_preserves_plaintext_and_clears_failed_decryption(monkeypatch, config, decrypt_api_key, expected):
    monkeypatch.setattr(message_platform_model, "decrypt_api_key", decrypt_api_key)
    platform = MessagePlatform(
        id=1,
        name="platform",
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        config=config,
    )

    response = MessagePlatformResponse.model_validate(platform)

    assert response.config == expected


@pytest.mark.asyncio
async def test_manager_start_is_idempotent_and_stop_clears_runtime_tasks(monkeypatch):
    manager = MessagePlatformPollingManager(())
    stop_gate = asyncio.Event()

    async def wait_until_stopped():
        await stop_gate.wait()

    monkeypatch.setattr(manager, "_supervisor_loop", wait_until_stopped)
    monkeypatch.setattr(manager, "_outbox_loop", wait_until_stopped)

    manager.start()
    first_supervisor = manager._supervisor_task
    first_outbox = manager._outbox_task
    manager.start()

    assert manager.is_running is True
    assert manager._supervisor_task is first_supervisor
    assert manager._outbox_task is first_outbox

    await manager.stop()

    assert manager.is_running is False
    assert manager._supervisor_task is None
    assert manager._outbox_task is None
