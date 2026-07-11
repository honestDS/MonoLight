import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.schemas import WeixinOpenClawMessage
from app.core.message_platforms.weixin_openclaw import WeixinOpenClawPlatformHandler


@pytest.mark.asyncio
async def test_messages_from_separate_polls_are_dispatched_immediately(monkeypatch):
    handler = WeixinOpenClawPlatformHandler()
    adapter = SimpleNamespace(config=SimpleNamespace(merge_single_poll_messages=True))
    active_message_tasks: set[asyncio.Task] = set()
    started_messages: list[str] = []
    release = asyncio.Event()

    async def handle_message(_adapter, message, *, uid, platform_id, adapter_signature):
        started_messages.append(message.text)
        await release.wait()

    monkeypatch.setattr(handler, "_handle_message", handle_message)

    for text in ("first", "second"):
        handler._enqueue_message(
            adapter,
            WeixinOpenClawMessage(
                user_id="weixin-user",
                text=text,
                session_id="weixin-openclaw:weixin-user",
            ),
            uid="owner",
            platform_id=1,
            adapter_signature=("adapter",),
            active_message_tasks=active_message_tasks,
        )

    await asyncio.sleep(0)

    assert started_messages == ["first", "second"]
    assert len(active_message_tasks) == 2

    release.set()
    await asyncio.gather(*active_message_tasks)

