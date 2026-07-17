import json
from typing import Any

from app.core.audit.service import classify_audit_score
from app.core.constants import ERR_AUDIT_HIGH_RISK_BLOCKED, ERR_AUDIT_SECURITY_BLOCKED, ERR_AUDIT_SYSTEM_UNAVAILABLE, ERR_TOOL_SHELL_BLACKLISTED
from app.core.crud.channel import channel_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import AUDIT_PROMPT
from app.core.tools import get_registered_tool_names
from app.core.tools.shell import ShellExecutor
from app.models.audit import AuditToolConclusion
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


async def audit_command(command: str, provider_url: str, api_key: str, model_id: str, session_id: str | None = None, uid: str | None = None) -> dict[str, Any] | None:
    try:
        messages = [
            InternalMessage(role=MessageRole.SYSTEM, content=AUDIT_PROMPT),
            InternalMessage(role=MessageRole.USER, content=f"Command to analyze: {command}"),
        ]
        result = await LLMClient.generate(
            api_key=api_key,
            base_url=provider_url,
            model_id=model_id,
            messages=messages,
            temperature=0.1,
        )
        content = (result.message.content or "").strip()
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            return None
        parsed = json.loads(content[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.bind(uid=uid, session_id=session_id, security=True).error(t("LOG_AUDIT_EXCEPTION", error=str(exc)))
        return None


class AuditMiddleware:
    @staticmethod
    async def audit(db, profile, cfg, tool_name: str, args: dict, session_id: str | None = None, uid: str | None = None) -> str | None:
        if tool_name not in get_registered_tool_names() or tool_name not in {"execute_shell", "write_file"}:
            return None

        if tool_name == "execute_shell":
            command = str(args.get("command", ""))
            blacklisted = ShellExecutor.check_blacklist(command)
            if blacklisted:
                return json.dumps({"error": t(ERR_AUDIT_SECURITY_BLOCKED), "reason": t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted)}, ensure_ascii=False)
        else:
            command = f"Write to {args.get('file_path', '')}: {args.get('content', '')}"

        if not cfg.security.audit_channel_id or not cfg.security.audit_model_id:
            return None
        channel = await channel_crud.get(db, cfg.security.audit_channel_id)
        if channel is None or not channel.is_active:
            return json.dumps({"error": "audit_system_failure", "reason": t(ERR_AUDIT_SYSTEM_UNAVAILABLE)}, ensure_ascii=False)

        await db.commit()
        audit_result = await audit_command(
            command,
            channel.base_url,
            channel.get_decrypted_api_key(),
            cfg.security.audit_model_id,
            session_id=session_id,
            uid=uid,
        )
        if audit_result is None:
            return json.dumps({"error": "audit_system_failure", "reason": t(ERR_AUDIT_SYSTEM_UNAVAILABLE)}, ensure_ascii=False)
        score = audit_result.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 10:
            return json.dumps({"error": "audit_system_failure", "reason": t(ERR_AUDIT_SYSTEM_UNAVAILABLE)}, ensure_ascii=False)
        conclusion = classify_audit_score(score, cfg.security.audit_threshold)
        if conclusion == AuditToolConclusion.BLOCKED:
            return json.dumps({"error": t(ERR_AUDIT_SECURITY_BLOCKED), "reason": t(ERR_AUDIT_HIGH_RISK_BLOCKED, score=score)}, ensure_ascii=False)
        if conclusion == AuditToolConclusion.PENDING:
            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": "该操作必须等待当前用户通过服务端确认，模型重试或修改参数不能放行",
                    "risky_command": command,
                    "score": score,
                },
                ensure_ascii=False,
            )
        return None
