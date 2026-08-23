from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import Any

from sqlalchemy import JSON, Integer, String, column, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_SYSTEM_SECRETS_MIGRATION_INVALID
from app.core.system_secrets import (
    SystemSecrets,
    SystemSecretsError,
    load_system_secrets,
    replace_system_secrets,
)

MIGRATION_ID = "20260823_rotate_system_secrets"

_ENCRYPTED_PREFIX = "enc:v1:"
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_LEGACY_DEFAULT_JWT = "UGNOvsDh371_1upgMs8yGgZHELcW-FTL64QvvHSJHKc"
_LEGACY_DEFAULT_KEY = bytes.fromhex("a3f7b2c8e9d4f1a6b5c3d7e2f8a1b4c6d9e3f7a2b8c5d1e6f3a7b2c9d4e8f1a5")

_CHANNEL = table(
    "channel",
    column("id", Integer),
    column("api_key", String),
)
_MESSAGE_PLATFORM = table(
    "message_platform",
    column("id", Integer),
    column("config", JSON),
)


def _raise_migration_invalid() -> None:
    raise SystemSecretsError(ERR_SYSTEM_SECRETS_MIGRATION_INVALID)


def _is_hex_key(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _source_keys(current_key: bytes) -> tuple[bytes, ...]:
    candidates: list[bytes] = [current_key]
    environment_key = os.environ.get("MONOLIGH_ENCRYPTION_KEY")
    if environment_key is not None:
        if not _is_hex_key(environment_key):
            _raise_migration_invalid()
        candidates.append(bytes.fromhex(environment_key))
    candidates.append(_LEGACY_DEFAULT_KEY)

    seen: set[bytes] = set()
    unique_candidates: list[bytes] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)
    return tuple(unique_candidates)


def _decode_ciphertext(value: str) -> bytes | None:
    encoded = value[len(_ENCRYPTED_PREFIX) :]
    if not encoded:
        return None

    try:
        ciphertext = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError, TypeError, binascii.Error):
        return None
    return ciphertext or None


def _decrypt_value(ciphertext: bytes, key: bytes) -> str | None:
    if not key:
        return None

    plaintext_bytes = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(ciphertext))
    try:
        plaintext = plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None

    if not plaintext.strip() or not all(char.isprintable() for char in plaintext):
        return None
    return plaintext


def _reencrypted_value(
    value: object,
    source_keys: tuple[bytes, ...],
    replacement_key: bytes,
) -> tuple[str | None, bool]:
    if not isinstance(value, str) or not value.startswith(_ENCRYPTED_PREFIX):
        return None, False

    ciphertext = _decode_ciphertext(value)
    if ciphertext is None:
        return None, False

    plaintext: str | None = None
    for source_key in source_keys:
        plaintext = _decrypt_value(ciphertext, source_key)
        if plaintext is not None:
            if source_key == replacement_key:
                return None, False
            break
    if plaintext is None:
        return None, True

    encrypted = bytes(byte ^ replacement_key[index % len(replacement_key)] for index, byte in enumerate(plaintext.encode("utf-8")))
    return f"{_ENCRYPTED_PREFIX}{base64.b64encode(encrypted).decode('ascii')}", False


def _replacement_secrets(
    current: SystemSecrets,
    source_keys: tuple[bytes, ...],
) -> SystemSecrets:
    jwt_secret_key = current.jwt_secret_key
    if current.jwt_secret_key == _LEGACY_DEFAULT_JWT:
        jwt_secret_key = secrets.token_urlsafe(48)
        while jwt_secret_key == current.jwt_secret_key:
            jwt_secret_key = secrets.token_urlsafe(48)

    channel_encryption_key = current.channel_encryption_key
    if current.channel_encryption_key == _LEGACY_DEFAULT_KEY:
        source_key_values = set(source_keys)
        channel_encryption_key = secrets.token_bytes(32)
        while channel_encryption_key in source_key_values:
            channel_encryption_key = secrets.token_bytes(32)

    return SystemSecrets(jwt_secret_key, channel_encryption_key)


async def _collect_updates(
    session: AsyncSession,
    source_keys: tuple[bytes, ...],
    replacement_key: bytes,
) -> tuple[list[tuple[Any, str]], list[tuple[Any, dict[str, Any]]], bool]:
    channel_updates: list[tuple[Any, str]] = []
    message_platform_updates: list[tuple[Any, dict[str, Any]]] = []
    unrecovered = False

    channel_result = await session.execute(select(_CHANNEL.c.id, _CHANNEL.c.api_key))
    for row in channel_result.mappings():
        replacement, value_unrecovered = _reencrypted_value(row["api_key"], source_keys, replacement_key)
        unrecovered = unrecovered or value_unrecovered
        if replacement is not None:
            channel_updates.append((row["id"], replacement))

    message_platform_result = await session.execute(select(_MESSAGE_PLATFORM.c.id, _MESSAGE_PLATFORM.c.config))
    for row in message_platform_result.mappings():
        config = row["config"]
        if not isinstance(config, dict):
            continue

        updated_config: dict[str, Any] = dict(config)
        changed = False
        for key in ("token", "bot_token"):
            replacement, value_unrecovered = _reencrypted_value(config.get(key), source_keys, replacement_key)
            unrecovered = unrecovered or value_unrecovered
            if replacement is not None:
                updated_config[key] = replacement
                changed = True

        if changed:
            message_platform_updates.append((row["id"], updated_config))

    return channel_updates, message_platform_updates, unrecovered


async def migrate(session: AsyncSession) -> None:
    current = load_system_secrets()
    source_keys = _source_keys(current.channel_encryption_key)
    replacement = _replacement_secrets(current, source_keys)
    channel_updates, message_platform_updates, unrecovered = await _collect_updates(session, source_keys, replacement.channel_encryption_key)
    if unrecovered:
        _raise_migration_invalid()

    for row_id, api_key in channel_updates:
        await session.execute(update(_CHANNEL).where(_CHANNEL.c.id == row_id).values(api_key=api_key))
    for row_id, config in message_platform_updates:
        await session.execute(update(_MESSAGE_PLATFORM).where(_MESSAGE_PLATFORM.c.id == row_id).values(config=config))

    if replacement != current:
        replace_system_secrets(current, replacement)
