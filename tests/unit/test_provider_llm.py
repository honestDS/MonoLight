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


def _encrypted_key(monkeypatch, plain_text: str = "plain-key") -> str:
    from app.core.crypto import encrypt_api_key

    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)
    return encrypt_api_key(plain_text)


@pytest.mark.asyncio
async def test_generate_success(monkeypatch):
    encrypted_key = _encrypted_key(monkeypatch)
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(return_value=_mock_chat_response()),
    ) as mock_generate:
        result = await LLMClient.generate(api_key=encrypted_key, base_url="u", model_id="m", messages=[])
        assert result.message.content == "hello"
        assert mock_generate.await_args.kwargs["api_key"] == "plain-key"


@pytest.mark.asyncio
async def test_generate_connection_error(monkeypatch):
    from app.core.exceptions import LLMException

    encrypted_key = _encrypted_key(monkeypatch)
    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(side_effect=LLMException("Connection Error")),
    ):
        with pytest.raises(LLMException) as exc:
            await LLMClient.generate(api_key=encrypted_key, base_url="u", model_id="m", messages=[])
        assert "Connection Error" in str(exc.value)


@pytest.mark.asyncio
async def test_generate_decrypts_encrypted_api_key_once(monkeypatch):
    encrypted_key = _encrypted_key(monkeypatch)

    with patch(
        "app.transformers.openai.OpenAITransformer.generate",
        AsyncMock(return_value=_mock_chat_response()),
    ) as mock_generate:
        result = await LLMClient.generate(api_key=encrypted_key, base_url="u", model_id="m", messages=[])

    assert result.message.content == "hello"
    assert mock_generate.await_args.kwargs["api_key"] == "plain-key"


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
