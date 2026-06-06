import json
from unittest.mock import AsyncMock, patch

import pytest
from app.schemas.message import InternalMessage, InternalResponse, MessageRole

from app.core.middleware.auditor import audit_command


@pytest.mark.asyncio
async def test_audit_command_success():
    mock_resp = InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=json.dumps({"score": 2, "reason": "Safe"}),
        ),
        model="m1",
    )
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=mock_resp)):
        result = await audit_command("ls", "url", "key", "m1")
        assert result["score"] == 2


@pytest.mark.asyncio
async def test_audit_command_markdown_json_success():
    mock_content = '```json\n{"score": 0, "reason": "V"}\n```'
    mock_resp = InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=mock_content),
        model="m1",
    )
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=mock_resp)):
        result = await audit_command("ls", "url", "key", "m1")
        assert result["score"] == 0
