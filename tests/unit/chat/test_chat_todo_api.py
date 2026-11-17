from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import chat as chat_api


@pytest.mark.asyncio
async def test_session_todo_returns_current_plan_for_session_owner(monkeypatch):
    session = SimpleNamespace(session_id="session-1", uid="user-1")
    plan = SimpleNamespace(
        revision=4,
        todos=[
            {"content": "inspect layout", "status": "completed"},
            {"content": "build todo panel", "status": "in_progress"},
        ],
    )
    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", AsyncMock(return_value=session))
    get_plan = AsyncMock(return_value=plan)
    monkeypatch.setattr(chat_api.session_todo_crud, "get_by_session_id", get_plan)
    db = AsyncMock()

    response = await chat_api.get_session_todo(
        session_id="session-1",
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert response.data == {
        "revision": 4,
        "todos": [
            {"content": "inspect layout", "status": "completed"},
            {"content": "build todo panel", "status": "in_progress"},
        ],
    }
    get_plan.assert_awaited_once_with(
        db,
        uid="user-1",
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_session_todo_rejects_other_user(monkeypatch):
    session = SimpleNamespace(session_id="session-1", uid="user-2")
    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", AsyncMock(return_value=session))
    get_plan = AsyncMock()
    monkeypatch.setattr(chat_api.session_todo_crud, "get_by_session_id", get_plan)

    response = await chat_api.get_session_todo(
        session_id="session-1",
        db=AsyncMock(),
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code != 200
    get_plan.assert_not_awaited()
