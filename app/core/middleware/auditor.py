import hashlib
import json
from typing import (
    Any,
)

from app.core.constants import (
    ERR_AUDIT_HIGH_RISK_BLOCKED,
    ERR_AUDIT_SECURITY_BLOCKED,
    ERR_AUDIT_SYSTEM_UNAVAILABLE,
    ERR_AUDIT_TOKEN_MISMATCH,
    ERR_TOOL_SHELL_BLACKLISTED,
)
from app.core.crud.channel import channel_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import (
    AUDIT_PROMPT,
    CONFIRMATION_NOTICE_PROMPT,
    CONFIRMATION_PREFIX,
    FILE_WRITE_CONFIRMATION_PROMPT,
)
from app.core.tools import get_registered_tool_names
from app.core.tools.shell import ShellExecutor
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
        logger.bind(uid=uid, session_id=session_id, security=True).error(t("LOG_AUDIT_EXCEPTION", error=str(e)))
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
                logger.bind(uid=uid, session_id=session_id, security=True).info(t("LOG_AUDIT_TOKEN_VERIFIED_COMMAND", command_preview=real_cmd[:30]))
                return True, real_cmd
            else:
                logger.bind(uid=uid, session_id=session_id, security=True).warning(t("LOG_AUDIT_TOKEN_MISMATCH", expected_token=expected_token, token=token))
                return False, real_cmd
        return None, command

    @staticmethod
    async def audit(db, profile, cfg, tool_name: str, args: dict, session_id: str = None, uid: str = None) -> str | None:
        if tool_name not in get_registered_tool_names():
            return None

        # 仅对特定工具进行安全审计
        if tool_name not in ["execute_shell", "write_file"]:
            return None

        is_any_verified = None
        command = ""

        # 提取 execute_shell 的内容并检查验证状态
        if tool_name == "execute_shell":
            command = args.get("command", "")

            blacklisted = ShellExecutor.check_blacklist(command)
            if blacklisted:
                return json.dumps(
                    {
                        "error": t(ERR_AUDIT_SECURITY_BLOCKED),
                        "reason": t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted),
                    },
                    ensure_ascii=False,
                )

            is_any_verified, command = AuditMiddleware.verify_token(command, session_id=session_id, uid=uid)

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
                    logger.bind(uid=uid, session_id=session_id, security=True).info(t("LOG_AUDIT_TOKEN_VERIFIED_WRITE_FILE", path=clean_path))
                    is_any_verified = True
                else:
                    logger.bind(uid=uid, session_id=session_id, security=True).warning(t("LOG_AUDIT_TOKEN_MISMATCH_WRITE_FILE", expected_token=expected_token, token=provided_token))
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
                    "reason": t(ERR_AUDIT_TOKEN_MISMATCH, expected_token=hashlib.sha256(command.strip().encode()).hexdigest()[:12]),
                },
                ensure_ascii=False,
            )

        if cfg.security.audit_threshold == 0:
            return None

        if not cfg.security.audit_channel_id or cfg.security.audit_channel_id <= 0:
            return None

        channel = await channel_crud.get(db, cfg.security.audit_channel_id)
        if not channel:
            return None
        # 审计模型被禁用时跳过安全审计（视为用户主动停用该审计能力），不阻断对话主线
        if not channel.is_active:
            logger.bind(uid=uid, session_id=session_id, security=True).warning(t("LOG_AUDIT_PROVIDER_DISABLED"))
            return None

        await db.commit()
        audit_res = await audit_command(command, channel.base_url, channel.get_decrypted_api_key(), cfg.security.audit_model_id, session_id=session_id, uid=uid)

        if audit_res is None:
            return json.dumps(
                {"error": "audit_system_failure", "reason": t(ERR_AUDIT_SYSTEM_UNAVAILABLE)},
                ensure_ascii=False,
            )

        score = audit_res.get("score", 10)
        audit_res.get("reason", "Unknown")

        if score >= 8:
            return json.dumps({"error": t(ERR_AUDIT_SECURITY_BLOCKED), "reason": t(ERR_AUDIT_HIGH_RISK_BLOCKED, score=score)}, ensure_ascii=False)
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
