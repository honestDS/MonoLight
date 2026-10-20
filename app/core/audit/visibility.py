from typing import Any

from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
    ERR_AUDIT_ROUND_BLOCKED,
    ERR_AUDIT_SERVICE_FAILED_STOP,
    ERR_TOOL_SHELL_BLACKLISTED,
    MSG_AUDIT_STATUS_CANCELLED,
    MSG_AUDIT_STATUS_EXECUTING,
    MSG_AUDIT_STATUS_EXPIRED,
    MSG_AUDIT_WAITING_CONFIRMATION,
)
from app.core.i18n import t
from app.core.i18n.locale import SUPPORTED_LOCALES


def obscure_audit_tool_result(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    # 显式类型消息由其业务协议处理。
    if "type" in payload:
        return payload

    status = payload.get("status")
    error_keys = {
        "blocked": ERR_AUDIT_ROUND_BLOCKED,
        "audit_failed": ERR_AUDIT_SERVICE_FAILED_STOP,
        "pending": MSG_AUDIT_WAITING_CONFIRMATION,
        "rejected": ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
        "expired": MSG_AUDIT_STATUS_EXPIRED,
        "cancelled": MSG_AUDIT_STATUS_CANCELLED,
        "executing": MSG_AUDIT_STATUS_EXECUTING,
    }
    if not isinstance(status, str) or status not in error_keys:
        return payload

    # 隐藏审计依据是刻意减少信息暴露，防止主LLM恶意绕过；用户决定和公开工具限制不属于隐藏范围。
    result = {"status": status, "error": t(error_keys[status])}
    if status == "blocked":
        from app.core.tools.shell import ShellExecutor

        for blacklisted in ShellExecutor.COMMAND_BLACKLIST:
            if any(payload.get("error") == t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted, locale=locale) for locale in SUPPORTED_LOCALES):
                public_restriction = t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted)
                result["error"] = public_restriction
                result["reason"] = public_restriction
                break
    if status in {"rejected", "expired", "cancelled", "executing"}:
        for field in ("confirmation_status", "confirmation_decision", "rejection_source"):
            value = payload.get(field)
            if isinstance(value, str):
                result[field] = value
    if status == "rejected":
        result["reason"] = t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER)
    return result
