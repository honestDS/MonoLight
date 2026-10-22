import json
from pathlib import Path

from app.core.constants import (
    MSG_AUDIT_CONFIRMATION_IM,
    MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM,
    MSG_AUDIT_WAITING_CONFIRMATION,
    MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS,
)
from app.core.i18n import t
from app.models.message import InternalMessage, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse, SentFile


def image_generation_payload(
    model: str,
    *,
    b64_json: str | None = None,
    url: str | None = None,
) -> dict:
    if (b64_json is None) == (url is None):
        raise ValueError("Exactly one of b64_json or url must be provided")

    image = {"b64_json": b64_json} if b64_json is not None else {"url": url}
    return {"model": model, "data": [image]}


def multimodal_read_payload(
    path,
    *,
    modality="image",
    message=None,
    error=None,
) -> dict:
    resolved_path = str(Path(path).resolve())
    if error is not None:
        return {
            "type": "multimodal_file_read",
            "status": "failed",
            "error": error,
            "path": resolved_path,
        }

    return {
        "type": "multimodal_file_read",
        "status": "success",
        "modality": modality,
        "path": resolved_path,
        "message": t(MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS) if message is None else message,
    }


def llm_response_payload(
    content: str | None = "",
    *,
    files: list[dict] | None = None,
    history: list[dict] | None = None,
) -> dict:
    return LLMResponse(
        choices=[
            LLMChoice(
                message=LLMChoiceMessage(role="assistant", content=content),
                finish_reason=True,
                created_at=0.0,
            )
        ],
        files=[SentFile.model_validate(item) for item in files] if files is not None else None,
        history=history,
    ).model_dump(mode="json", exclude_none=True)


def knowledge_query_payload(items: list[tuple[str, str]]) -> dict:
    return {"items": [{"source": source, "content": content} for source, content in items]}


def memory_mutation_payload(
    operation: str,
    *,
    job_id: int,
    memory_id: int | None = None,
    current_version: int | None = None,
) -> dict:
    return {
        "operation": operation,
        "status": "accepted",
        "job_id": job_id,
        "memory_id": memory_id,
        "current_version": current_version,
    }


def memory_content_too_long_payload(
    operation: str,
    *,
    actual_tokens: int,
    max_tokens: int = 160,
) -> dict:
    return {
        "operation": operation,
        "status": "content_too_long",
        "actual_tokens": actual_tokens,
        "max_tokens": max_tokens,
        "retryable": True,
    }


def audit_confirmation_payload(
    audit_record_id: int,
    *,
    summary: str = "Confirm command",
    risk: int = 5,
    expires_at: str = "2026-07-18T17:00:25",
    timeout_seconds: int = 25,
    confirmation_mode: str = "standard",
    locale: str = "zh",
) -> dict:
    return {
        "type": "audit_confirmation",
        "audit_record_id": audit_record_id,
        "summary": summary,
        "risk": risk,
        "status": "pending",
        "confirmation_mode": confirmation_mode,
        "expires_at": expires_at,
        "plain_text": t(
            MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM if confirmation_mode == "high_risk_override" else MSG_AUDIT_CONFIRMATION_IM,
            locale=locale,
            summary=summary,
            score=risk,
            expires_in_seconds=timeout_seconds,
        ),
    }


def pending_audit_tool_result(tool_call_id: str) -> InternalMessage:
    return InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call_id,
        content=json.dumps(
            {
                "status": "pending",
                "error": t(MSG_AUDIT_WAITING_CONFIRMATION),
            },
            ensure_ascii=False,
        ),
    )
