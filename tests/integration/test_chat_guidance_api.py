from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.api.v1.chat import router
from app.core.constants import GUIDANCE_MESSAGE_PREFIX, GUIDANCE_MESSAGE_SUFFIX
from app.core.security import get_current_user
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.providers.database import get_db


@pytest.mark.asyncio
async def test_create_session_guidance_via_api(db_session: AsyncSession):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return SimpleNamespace(uid="user-1")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    db_session.add(
        ChatSession(
            session_id="external-session-1",
            uid="user-1",
            profile_id=7,
            source="weixin-openclaw",
            reply_target_source="weixin-openclaw",
        )
    )
    await db_session.commit()

    expected_content = f"{GUIDANCE_MESSAGE_PREFIX}请先回答重点{GUIDANCE_MESSAGE_SUFFIX}"

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/chat/sessions/guidance",
                json={
                    "session_id": "external-session-1",
                    "content": " 请先回答重点 ",
                },
            )

        payload = response.json()
        assert response.status_code == 200
        assert payload["code"] == 200
        assert payload["data"]["type"] == "guidance"
        assert payload["data"]["role"] == "system"
        assert payload["data"]["profile_id"] == 7
        assert payload["data"]["is_processed"] is False
        assert payload["data"]["content"] == expected_content

        result = await db_session.execute(select(Message))
        messages = list(result.scalars().all())
        assert len(messages) == 1

        message = messages[0]
        assert message.session_id == "external-session-1"
        assert message.uid == "user-1"
        assert message.profile_id == 7
        assert message.role == MessageRole.SYSTEM
        assert message.type == MessageType.GUIDANCE
        assert message.content == expected_content
        assert message.is_processed is False
    finally:
        app.dependency_overrides.clear()
