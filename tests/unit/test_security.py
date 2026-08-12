from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import security
from app.core.constants import ERR_UNAUTHORIZED


class _AsyncSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


@pytest.fixture(autouse=True)
def _patch_jwt_environment(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")


def _patch_user_lookup(monkeypatch, user):
    monkeypatch.setattr(security, "AsyncSessionLocal", _AsyncSessionContext)

    async def get_by_username(*, db, username):
        return user

    monkeypatch.setattr(security.user_crud, "get_by_username", get_by_username)


def _token_for(username):
    return security.create_access_token(data={"sub": username})


@pytest.mark.asyncio
async def test_get_current_user_returns_active_user(monkeypatch):
    user = SimpleNamespace(username="alice", is_active=True)
    _patch_user_lookup(monkeypatch, user)

    result = await security.get_current_user(_token_for(user.username))

    assert result is user


@pytest.mark.asyncio
async def test_get_current_user_rejects_missing_user(monkeypatch):
    _patch_user_lookup(monkeypatch, None)

    with pytest.raises(HTTPException) as exc_info:
        await security.get_current_user(_token_for("missing"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == ERR_UNAUTHORIZED
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_get_current_user_rejects_disabled_user(monkeypatch):
    user = SimpleNamespace(username="disabled", is_active=False)
    _patch_user_lookup(monkeypatch, user)

    with pytest.raises(HTTPException) as exc_info:
        await security.get_current_user(_token_for(user.username))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == ERR_UNAUTHORIZED
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}
