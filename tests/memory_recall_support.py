import json
from pathlib import Path
from typing import Any


def memory_recall_payload(*, current_session_id: str | None = "session-1", empty: bool = False) -> dict[str, Any]:
    payload = json.loads(Path(__file__).with_name("memory_recall_fixture.json").read_text(encoding="utf-8"))
    payload["current_session_id"] = current_session_id

    if empty:
        payload["memories"] = []
        payload.pop("knowledge_base")
        payload.pop("chat_history")

    return payload
