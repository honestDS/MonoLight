import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_SETUP_SESSION_INVALID,
    ERR_SETUP_SESSION_STATE_INVALID,
    SETUP_SESSION_RECORD_VERSION,
    SETUP_SESSION_TTL_SECONDS,
)
from app.core.crud.system_setting import system_setting_crud
from app.core.exceptions import ForbiddenException, ServerException


@dataclass(frozen=True, slots=True)
class IssuedSetupSession:
    token: str
    max_age: int


_SETUP_SESSION_FIELDS = {"version", "token_hash", "expires_at"}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError
        record[key] = value
    return record


def _parse_setup_session_record(value: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID)

    try:
        record = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError):
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID) from None

    if not isinstance(record, dict) or set(record) != _SETUP_SESSION_FIELDS:
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID)

    version = record["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != SETUP_SESSION_RECORD_VERSION:
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID)

    token_hash = record["token_hash"]
    if not isinstance(token_hash, str) or len(token_hash) != 64 or not all(character in "0123456789abcdefABCDEF" for character in token_hash):
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID)

    expires_at = record["expires_at"]
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= 0:
        raise ServerException(ERR_SETUP_SESSION_STATE_INVALID)

    return token_hash, expires_at


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_matches(token: str | None, expected_hash: str) -> bool:
    if not isinstance(token, str):
        return False
    return hmac.compare_digest(_hash_token(token), expected_hash)


def _new_session(now: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    record = json.dumps(
        {
            "expires_at": now + SETUP_SESSION_TTL_SECONDS,
            "token_hash": token_hash,
            "version": SETUP_SESSION_RECORD_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return token, record


async def _reject_concurrent_establishment(db: AsyncSession) -> None:
    await db.rollback()
    raise ForbiddenException(ERR_SETUP_SESSION_INVALID)


async def establish_setup_session(db: AsyncSession, token: str | None) -> IssuedSetupSession | None:
    record = await system_setting_crud.get_setup_session_record(db)
    now = int(time.time())

    if record is not None:
        token_hash, expires_at = _parse_setup_session_record(record)
        if expires_at > now:
            if _token_matches(token, token_hash):
                return None
            raise ForbiddenException(ERR_SETUP_SESSION_INVALID)

    issued_token, candidate_record = _new_session(now)
    if record is None:
        final_value = await system_setting_crud.initialize_setup_session_record(db, value=candidate_record)
        if final_value != candidate_record:
            await _reject_concurrent_establishment(db)
    elif not await system_setting_crud.replace_setup_session_record(
        db,
        expected_value=record,
        new_value=candidate_record,
    ):
        await _reject_concurrent_establishment(db)

    await db.commit()
    return IssuedSetupSession(token=issued_token, max_age=SETUP_SESSION_TTL_SECONDS)


async def require_setup_session(db: AsyncSession, token: str | None) -> None:
    record = await system_setting_crud.get_setup_session_record(db)
    if record is None:
        raise ForbiddenException(ERR_SETUP_SESSION_INVALID)

    token_hash, expires_at = _parse_setup_session_record(record)
    if expires_at <= int(time.time()) or not _token_matches(token, token_hash):
        raise ForbiddenException(ERR_SETUP_SESSION_INVALID)
