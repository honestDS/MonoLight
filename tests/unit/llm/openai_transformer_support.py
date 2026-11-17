import json
from typing import Any

import pytest

from app.core.utils import llm_request_policy


class _AsyncBytesIterator:
    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeAiohttpResponse:
    def __init__(self, *, text: str = "", chunks: list[bytes] | None = None, status: int = 200):
        self.status = status
        self._text = text
        self.content = self
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self) -> str:
        return self._text

    def iter_any(self):
        return _AsyncBytesIterator(self._chunks)


class _FakeClientSession:
    def __init__(self, response: _FakeAiohttpResponse):
        self._response = response
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "kwargs": kwargs})
        return self._response


@pytest.fixture(autouse=True)
def _mock_llm_request_max_bytes(monkeypatch):
    async def fake_load_llm_request_max_bytes() -> int:
        return 32 * 1024 * 1024

    monkeypatch.setattr(llm_request_policy, "load_llm_request_max_bytes", fake_load_llm_request_max_bytes)


def _responses_sse_chunks(events: list[dict[str, Any]]) -> list[bytes]:
    return [f"data: {json.dumps(event)}\n\n".encode() for event in events] + [b"data: [DONE]\n\n"]
