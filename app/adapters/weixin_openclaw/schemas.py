from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.message import TextPart


@dataclass
class WeixinOpenClawMessage:
    user_id: str
    text: str | list[TextPart]
    session_id: str
    context_token: str = ""
    attachments: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class WeixinOpenClawChatResult:
    text: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
