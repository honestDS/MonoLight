import base64

import pytest

from app.core import crypto
from app.core.constants import (
    ERR_API_KEY_CRYPTO_KEY_INVALID,
    ERR_API_KEY_CRYPTO_KEY_MISSING,
    ERR_SYSTEM_SECRETS_FILE_INVALID,
    ERR_SYSTEM_SECRETS_FILE_MISSING,
)
from app.core.exceptions import ApiKeyException
from app.core.system_secrets import SystemSecretsError

CHANNEL_ENCRYPTION_KEY = b"0123456789abcdef0123456789abcdef"
PLAINTEXT_API_KEY = "mock-api-key-value"


def test_api_key_crypto_preserves_legacy_xor_base64_format(monkeypatch) -> None:
    monkeypatch.setattr(crypto, "get_channel_encryption_key", lambda: CHANNEL_ENCRYPTION_KEY)
    plain_bytes = PLAINTEXT_API_KEY.encode("utf-8")
    expected_ciphertext = base64.b64encode(bytes(byte ^ CHANNEL_ENCRYPTION_KEY[index % len(CHANNEL_ENCRYPTION_KEY)] for index, byte in enumerate(plain_bytes))).decode("utf-8")

    encrypted = crypto.encrypt_api_key(PLAINTEXT_API_KEY)

    assert encrypted == expected_ciphertext
    assert crypto.decrypt_api_key(encrypted) == PLAINTEXT_API_KEY


@pytest.mark.parametrize(
    ("system_secrets_error", "api_key_error"),
    [
        (ERR_SYSTEM_SECRETS_FILE_MISSING, ERR_API_KEY_CRYPTO_KEY_MISSING),
        (ERR_SYSTEM_SECRETS_FILE_INVALID, ERR_API_KEY_CRYPTO_KEY_INVALID),
    ],
)
def test_api_key_crypto_maps_system_secrets_errors_without_leaking_key(monkeypatch, caplog, system_secrets_error: str, api_key_error: str) -> None:
    def raise_system_secrets_error() -> None:
        raise SystemSecretsError(system_secrets_error)

    monkeypatch.setattr(crypto, "get_channel_encryption_key", raise_system_secrets_error)
    handler_id = crypto.logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with pytest.raises(ApiKeyException) as exc_info:
            crypto.encrypt_api_key(PLAINTEXT_API_KEY)
    finally:
        crypto.logger.remove(handler_id)

    exception_text = str(exc_info.value)
    assert exc_info.value.message == api_key_error
    assert api_key_error in caplog.text
    assert CHANNEL_ENCRYPTION_KEY.decode() not in exception_text
    assert PLAINTEXT_API_KEY not in exception_text
    assert CHANNEL_ENCRYPTION_KEY.decode() not in caplog.text
    assert PLAINTEXT_API_KEY not in caplog.text
