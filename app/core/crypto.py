"""轻量级加密工具：使用标准库实现API密钥加密存储

采用XOR+Base64方案，无需额外依赖。密钥存储在环境变量中。
虽不如AES-256强度高，但足以防止数据库文件泄露时的明文暴露。
"""

import base64
import binascii
import os

from app.core import constants
from app.core.exceptions import ApiKeyException
from app.core.log import get_logger

logger = get_logger(__name__)


def _raise_crypto_error(message: str, **kwargs) -> None:
    logger.bind(error_key=message, **kwargs).error(message)
    raise ApiKeyException(message=message, **kwargs)


def _get_encryption_key() -> bytes:
    """获取加密密钥，优先从环境变量读取。"""
    key_hex = (os.getenv("MONOLIGH_ENCRYPTION_KEY") or "").strip()
    if not key_hex:
        _raise_crypto_error(constants.ERR_API_KEY_CRYPTO_KEY_MISSING)

    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        _raise_crypto_error(constants.ERR_API_KEY_CRYPTO_KEY_INVALID)

    if len(key) != 32:
        _raise_crypto_error(constants.ERR_API_KEY_CRYPTO_KEY_INVALID)

    return key


def encrypt_api_key(plain_text: str) -> str:
    """加密API密钥

    Args:
        plain_text: 明文API密钥

    Returns:
        Base64编码的密文
    """
    if not plain_text or not plain_text.strip():
        _raise_crypto_error(constants.ERR_API_KEY_ENCRYPT_INPUT_EMPTY)

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
        _raise_crypto_error(constants.ERR_API_KEY_DECRYPT_INPUT_EMPTY)

    key = _get_encryption_key()

    try:
        encrypted_bytes = base64.b64decode(encrypted_text.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError):
        _raise_crypto_error(constants.ERR_API_KEY_DECRYPT_FAILED)

    try:
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key[i % len(key)])

        plain_text = bytes(decrypted).decode("utf-8")
    except UnicodeDecodeError:
        _raise_crypto_error(constants.ERR_API_KEY_DECRYPT_FAILED)

    if not plain_text.strip():
        _raise_crypto_error(constants.ERR_API_KEY_DECRYPT_EMPTY)

    return plain_text


def mask_api_key(api_key: str) -> str:
    """脱敏API密钥用于日志和响应

    Args:
        api_key: 原始API密钥（明文或密文）

    Returns:
        脱敏后的字符串
    """
    if not api_key or len(api_key) < 8:
        return "****"
    return api_key[:8] + "****"
