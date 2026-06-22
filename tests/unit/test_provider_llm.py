from unittest.mock import AsyncMock, patch

import pytest

from app.core import constants
from app.core.exceptions import ApiKeyException
from app.providers.llm.client import LLMClient


def _mock_chat_response():
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                }
            }
        ],
        "model": "test",
    }


@pytest.mark.asyncio
async def test_generate_success():
    plain_key = "plain-key"
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(return_value=_mock_chat_response()),
    ) as mock_generate:
        result = await LLMClient.generate(api_key=plain_key, base_url="u", model_id="m", messages=[])
        assert result.message.content == "hello"
        assert mock_generate.await_args.kwargs["api_key"] == plain_key


@pytest.mark.asyncio
async def test_generate_connection_error():
    from app.core.exceptions import LLMException

    plain_key = "plain-key"
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(side_effect=LLMException("Connection Error")),
    ):
        with pytest.raises(LLMException) as exc:
            await LLMClient.generate(api_key=plain_key, base_url="u", model_id="m", messages=[])
        assert "Connection Error" in str(exc.value)


@pytest.mark.asyncio
async def test_generate_passes_caller_api_key_to_transformer():
    plain_key = "plain-key"

    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(return_value=_mock_chat_response()),
    ) as mock_generate:
        result = await LLMClient.generate(api_key=plain_key, base_url="u", model_id="m", messages=[])

    assert result.message.content == "hello"
    assert mock_generate.await_args.kwargs["api_key"] == plain_key


def test_decrypt_api_key_fails_without_encryption_key(monkeypatch):
    from app.core.crypto import decrypt_api_key

    monkeypatch.delenv("MONOLIGH_ENCRYPTION_KEY", raising=False)

    with pytest.raises(ApiKeyException) as exc:
        decrypt_api_key("encrypted-key")

    assert exc.value.message == constants.ERR_API_KEY_CRYPTO_KEY_MISSING


def test_decrypt_api_key_rejects_plaintext(monkeypatch):
    from app.core.crypto import decrypt_api_key

    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)

    with pytest.raises(ApiKeyException) as exc:
        decrypt_api_key("plain-key")

    assert exc.value.message == constants.ERR_API_KEY_DECRYPT_FAILED


def test_decrypt_api_key_rejects_empty_plaintext(monkeypatch):
    from app.core.crypto import decrypt_api_key

    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)

    with pytest.raises(ApiKeyException) as exc:
        decrypt_api_key("")

    assert exc.value.message == constants.ERR_API_KEY_DECRYPT_INPUT_EMPTY
