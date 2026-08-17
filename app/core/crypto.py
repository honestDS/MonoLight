"""轻量级加密工具：使用标准库实现API密钥加密存储

采用XOR+Base64方案，无需额外依赖。渠道/API Key 加密密钥来自持久化系统密钥文件。
虽不如AES-256强度高，但足以防止数据库文件泄露时的明文暴露。
"""

import base64
import binascii

from app.core.constants import (
    ERR_API_KEY_CRYPTO_KEY_INVALID,
    ERR_API_KEY_CRYPTO_KEY_MISSING,
    ERR_API_KEY_DECRYPT_EMPTY,
    ERR_API_KEY_DECRYPT_FAILED,
    ERR_API_KEY_DECRYPT_INPUT_EMPTY,
    ERR_API_KEY_ENCRYPT_INPUT_EMPTY,
    ERR_SYSTEM_SECRETS_FILE_MISSING,
)
from app.core.exceptions import ApiKeyException
from app.core.log import get_logger
from app.core.system_secrets import SystemSecretsError, get_channel_encryption_key

logger = get_logger(__name__)


def _raise_crypto_error(message: str, **kwargs) -> None:
    logger.bind(error_key=message, **kwargs).error(message)
    raise ApiKeyException(message=message, **kwargs)


def _get_encryption_key() -> bytes:
    """获取持久化系统密钥文件中的渠道/API Key 加密密钥。"""
    try:
        return get_channel_encryption_key()
    except SystemSecretsError as error:
        if error.message_key == ERR_SYSTEM_SECRETS_FILE_MISSING:
            _raise_crypto_error(ERR_API_KEY_CRYPTO_KEY_MISSING)
        _raise_crypto_error(ERR_API_KEY_CRYPTO_KEY_INVALID)


def encrypt_api_key(plain_text: str) -> str:
    """加密API密钥

    Args:
        plain_text: 明文API密钥

    Returns:
        Base64编码的密文
    """
    if not plain_text or not plain_text.strip():
        _raise_crypto_error(ERR_API_KEY_ENCRYPT_INPUT_EMPTY)

    key = _get_encryption_key()
    plain_bytes = plain_text.encode("utf-8")

    encrypted = bytearray()
    for i, byte in enumerate(plain_bytes):
        encrypted.append(byte ^ key[i % len(key)])

    return base64.b64encode(bytes(encrypted)).decode("utf-8")


def decrypt_api_key(encrypted_text: str) -> str:
    """解密API密钥。

    Args:
        encrypted_text: Base64编码的密文

    Returns:
        明文API密钥
    """
    if not encrypted_text or not encrypted_text.strip():
        _raise_crypto_error(ERR_API_KEY_DECRYPT_INPUT_EMPTY)

    key = _get_encryption_key()

    try:
        encrypted_bytes = base64.b64decode(encrypted_text.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError):
        _raise_crypto_error(ERR_API_KEY_DECRYPT_FAILED)

    try:
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key[i % len(key)])

        plain_text = bytes(decrypted).decode("utf-8")
    except UnicodeDecodeError:
        _raise_crypto_error(ERR_API_KEY_DECRYPT_FAILED)

    if not plain_text.strip():
        _raise_crypto_error(ERR_API_KEY_DECRYPT_EMPTY)

    return plain_text
