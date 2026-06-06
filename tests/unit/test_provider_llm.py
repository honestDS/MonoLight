from unittest.mock import AsyncMock, patch

import pytest
from app.schemas.message import InternalMessage, InternalResponse, MessageRole

from app.providers.llm.client import LLMClient


@pytest.mark.asyncio
async def test_generate_success():
    mock_resp = InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content="hello"),
        model="test",
    )
    # LLMClient 调用的是 Transformer，所以 Mock Transformer.generate
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(return_value=mock_resp),
    ):
        result = await LLMClient.generate(api_key="k", base_url="u", model_id="m", messages=[])
        assert result.message.content == "hello"


@pytest.mark.asyncio
async def test_generate_connection_error():
    from app.core.exceptions import LLMException

    # 注意：Transformer 内部抛出异常时，LLMClient 应该透出
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(side_effect=LLMException("Connection Error")),
    ):
        with pytest.raises(LLMException) as exc:
            await LLMClient.generate(api_key="k", base_url="u", model_id="m", messages=[])
        assert "Connection Error" in str(exc.value)
