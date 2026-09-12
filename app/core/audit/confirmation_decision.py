from .confirmation_common import (
    _APPROVE_WORDS,
    _CONFIRMATION_CANDIDATE_WORDS,
    _IGNORE_WORDS,
    _REJECT_WORDS,
    ConfirmationDecision,
)

__all__ = [
    "is_confirmation_candidate",
    "parse_confirmation_decision",
    "message_has_quote",
]


def _normalize_confirmation_message(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
) -> str | None:
    if not isinstance(message, str) or attachments or has_quote:
        return None
    normalized = message.strip()
    if not normalized:
        return None
    return normalized.lower()


def is_confirmation_candidate(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
) -> bool:
    lowered = _normalize_confirmation_message(
        message,
        attachments=attachments,
        has_quote=has_quote,
    )
    return lowered in _CONFIRMATION_CANDIDATE_WORDS if lowered is not None else False


def parse_confirmation_decision(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
    requires_high_risk_override: bool = False,
) -> ConfirmationDecision | None:
    lowered = _normalize_confirmation_message(
        message,
        attachments=attachments,
        has_quote=has_quote,
    )
    if lowered is None:
        return None
    if lowered in _REJECT_WORDS:
        return ConfirmationDecision.REJECT
    if requires_high_risk_override:
        if lowered in _IGNORE_WORDS:
            return ConfirmationDecision.IGNORE
    elif lowered in _APPROVE_WORDS:
        return ConfirmationDecision.APPROVE
    return None


def message_has_quote(raw_message: object) -> bool:
    if not isinstance(raw_message, dict):
        return False
    quote_keys = {
        "quote",
        "quoted_message",
        "reference",
        "refer_message",
        "ref_message",
        "reply_to",
    }
    if any(raw_message.get(key) for key in quote_keys):
        return True
    item_list = raw_message.get("item_list")
    if not isinstance(item_list, list):
        return False
    return any(isinstance(item, dict) and (str(item.get("type") or "").lower() in {"quote", "reference", "reply"} or any(item.get(key) for key in quote_keys)) for item in item_list)
