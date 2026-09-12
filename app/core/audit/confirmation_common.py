from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.constants import (
    MSG_AUDIT_STATUS_CANCELLED,
    MSG_AUDIT_STATUS_EXECUTING,
    MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
    MSG_AUDIT_STATUS_EXPIRED,
    MSG_AUDIT_STATUS_FAILED,
    MSG_AUDIT_STATUS_REJECTED,
    MSG_AUDIT_STATUS_SUCCEEDED,
)
from app.core.log import get_logger
from app.models.message import InternalMessage

__all__ = [
    "ConfirmationDecision",
    "CONFIRMATION_DECISION_FIELD",
    "REJECTION_SOURCE_FIELD",
    "ConfirmationStatusUpdate",
    "ConfirmationMessageProjection",
    "PendingConfirmationCancellation",
]


class ConfirmationDecision(StrEnum):
    APPROVE = "approve"
    IGNORE = "ignore"
    REJECT = "reject"


_APPROVE_WORDS = {"同意", "继续", "approve", "continue"}

_IGNORE_WORDS = {"忽略", "ignore"}

_REJECT_WORDS = {"拒绝", "reject"}

_CONFIRMATION_CANDIDATE_WORDS = _APPROVE_WORDS | _IGNORE_WORDS | _REJECT_WORDS

CONFIRMATION_DECISION_FIELD = "confirmation_decision"

REJECTION_SOURCE_FIELD = "rejection_source"

_STATUS_TEXT_KEYS = {
    "executing": MSG_AUDIT_STATUS_EXECUTING,
    "rejected": MSG_AUDIT_STATUS_REJECTED,
    "expired": MSG_AUDIT_STATUS_EXPIRED,
    "cancelled": MSG_AUDIT_STATUS_CANCELLED,
    "succeeded": MSG_AUDIT_STATUS_SUCCEEDED,
    "failed": MSG_AUDIT_STATUS_FAILED,
    "execution_unknown": MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
}

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConfirmationStatusUpdate:
    record: Any
    message_id: int
    status: str
    content: str


@dataclass(frozen=True)
class ConfirmationMessageProjection:
    record: Any
    status_update: ConfirmationStatusUpdate | None


@dataclass(frozen=True)
class PendingConfirmationCancellation:
    tool_results: list[InternalMessage]
    status_update: ConfirmationStatusUpdate | None
