from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def aes_padded_size(size: int) -> int:
    return size + (16 - (size % 16) or 16)


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def parse_media_aes_key(aes_key_value: str) -> bytes:
    normalized = aes_key_value.strip()
    if not normalized:
        raise ValueError("empty media aes key")
    padded = normalized + "=" * (-len(normalized) % 4)
    decoded = base64.b64decode(padded)
    if len(decoded) == 16:
        return decoded
    decoded_text = decoded.decode("ascii", errors="ignore")
    if len(decoded) == 32 and all(c in "0123456789abcdefABCDEF" for c in decoded_text):
        return bytes.fromhex(decoded_text)
    raise ValueError("unsupported media aes key format")


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()
