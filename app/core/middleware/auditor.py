import hashlib
import json
from typing import (
    Any,
)

from app.core.log import get_logger
from app.core.prompts import (
    AUDIT_PROMPT,
    CONFIRMATION_NOTICE_PROMPT,
    CONFIRMATION_PREFIX,
    FILE_WRITE_CONFIRMATION_PROMPT,
)
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


async def audit_command(command: str, provider_url: str, api_key: str, model_id: str, session_id: str = None, uid: str = None) -> dict[str, Any] | None:
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

        content = result.message.content

        # 鲁棒性处理：剥离大模型可能输出的 Markdown JSON 标记
        clean_content = content.strip()
        if clean_content.startswith("```"):
            start = clean_content.find("{")
            end = clean_content.rfind("}")
            if start != -1 and end != -1:
                clean_content = clean_content[start : end + 1]
        elif "{" in clean_content and "}" in clean_content:
            start = clean_content.find("{")
            end = clean_content.rfind("}")
            clean_content = clean_content[start : end + 1]

        return json.loads(clean_content)
    except Exception as e:
        logger.bind(uid=uid, session_id=session_id, security=True).error(f"Audit Exception: {e}")
        return None


class AuditMiddleware:
    @staticmethod
    def _extract_token(text: str) -> tuple[str | None, str]:
        """从文本中提取确认 Token 和原始内容"""
        if text.startswith(CONFIRMATION_PREFIX):
            parts = text[len(CONFIRMATION_PREFIX) :].split(" ", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        return None, text

    @staticmethod
    def verify_token(command: str, session_id: str = None, uid: str = None) -> tuple[bool | None, str]:
        """
        验证指令的 Token。
        返回: (是否通过, 原始内容)
        其中 bool | None:
          - True: Token 存在且匹配
          - False: Token 存在但不匹配
          - None: Token 不存在
        """
        token, real_cmd = AuditMiddleware._extract_token(command)
        if token:
            expected_token = hashlib.sha256(real_cmd.strip().encode()).hexdigest()[:12]
            if token == expected_token:
                logger.bind(uid=uid, session_id=session_id, security=True).info(f"Dynamic token verification passed for command: {real_cmd[:30]}...")
                return True, real_cmd
            else:
                logger.bind(uid=uid, session_id=session_id, security=True).warning(f"Token mismatch! Expected: {expected_token}, Got: {token}")
                return False, real_cmd
        return None, command

    @staticmethod
    async def audit(db, profile, cfg, tool_name: str, args: dict, session_id: str = None, uid: str = None) -> str | None:
        from app.core.crud.provider import provider_crud
        from app.core.tools import get_registered_tool_names

        if tool_name not in get_registered_tool_names():
            return None

        # 仅对特定工具进行安全审计
        if tool_name not in ["execute_shell", "write_file"]:
            return None

        is_any_verified = None
        command = ""

        # 提取 execute_shell 的内容并检查验证状态
        if tool_name == "execute_shell":
            cmd_arg = args.get("command", "")
            is_any_verified, command = AuditMiddleware.verify_token(cmd_arg, session_id=session_id, uid=uid)

        # 提取 write_file 的内容并检查验证状态
        if tool_name == "write_file":
            path_arg = str(args.get("file_path", ""))
            content_arg = str(args.get("content", ""))

            # 提取可能存在的 Token
            token_p, clean_path = AuditMiddleware._extract_token(path_arg)
            token_c, clean_content = AuditMiddleware._extract_token(content_arg)

            # 合成审计指令字符串 (基于清理后的内容)
            command = f"Write to {clean_path}: {clean_content}"

            # 使用统一的 verify_token 逻辑，但由于是合成指令，我们需要手动调用校验
            # 注意：此处传入的是合成后的 command，而 Token 是从原参数中提取的
            expected_token = hashlib.sha256(command.strip().encode()).hexdigest()[:12]
            provided_token = token_p or token_c

            if provided_token:
                if provided_token == expected_token:
                    logger.bind(uid=uid, session_id=session_id, security=True).info(f"Dynamic token verification passed for write_file: {clean_path}")
                    is_any_verified = True
                else:
                    logger.bind(uid=uid, session_id=session_id, security=True).warning(f"Token mismatch for write_file! Expected: {expected_token}, Got: {provided_token}")
                    is_any_verified = False
            else:
                is_any_verified = None

        if not command:
            return None

        # 处理验证结果
        if is_any_verified is True:
            return None

        if is_any_verified is False:
            return json.dumps(
                {
                    "error": "token_mismatch",
                    "reason": f"Verification token mismatch. This usually happens if the command content was modified after the token was generated. Please use the EXACT command or request a new token. Expected token based on current content: {hashlib.sha256(command.strip().encode()).hexdigest()[:12]}",
                },
                ensure_ascii=False,
            )

        if cfg.security.audit_threshold == 0:
            return None

        if not cfg.security.audit_provider_id or cfg.security.audit_provider_id <= 0:
            return None

        provider = await provider_crud.get(db, cfg.security.audit_provider_id)
        if not provider:
            return None

        audit_res = await audit_command(command, provider.base_url, provider.api_key, cfg.security.audit_model_id, session_id=session_id, uid=uid)

        if audit_res is None:
            return json.dumps(
                {"error": "audit_system_failure", "reason": "Security Audit System is currently unavailable."},
                ensure_ascii=False,
            )

        score = audit_res.get("score", 10)
        audit_res.get("reason", "Unknown")

        if score >= 8:
            return json.dumps({"error": "Security Blocked", "reason": f"High risk score {score}: Security Blocked"}, ensure_ascii=False)

        if score >= cfg.security.audit_threshold:
            cmd_hash = hashlib.sha256(command.strip().encode()).hexdigest()[:12]
            dynamic_token = f"{CONFIRMATION_PREFIX}{cmd_hash}"

            # 为不同工具定制更精准的确认引导语
            if tool_name == "write_file":
                notice = FILE_WRITE_CONFIRMATION_PROMPT.format(score=score, dynamic_token=dynamic_token)
            else:
                notice = CONFIRMATION_NOTICE_PROMPT.format(score=score, dynamic_token=dynamic_token)

            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": notice,
                    "risky_command": command,
                    "dynamic_token": dynamic_token,
                },
                ensure_ascii=False,
            )
        return None
