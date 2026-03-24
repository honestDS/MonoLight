from app.core.prompts import CONFIRMATION_PREFIX
import json
import hashlib
from typing import (
    Dict,
    Any,
    Optional,
)
from app.core.log import get_logger
from app.providers.llm.client import LLMClient
from app.models.message import (
    InternalMessage,
    MessageRole,
)

logger = get_logger(__name__)

AUDIT_PROMPT = """
You are a DevOps and Security Compliance Auditor for an AI Self-Evolution framework. 
Analyze shell commands for security risks within the context of automated maintenance and refactoring.

Risk Scoring Strategy:
1. READ-ONLY operations (cat, ls, grep, find, etc.): SAFE (Score 0).
2. PERSISTENCE OF MALICIOUS INTENT: Any attempt to write (echo, tee, >, >>) content that represents system-destructive patterns, obfuscated payloads, or command fragments intended for later execution is CRITICAL (Score 8-10).
3. OBFUSCATION DETECTION: Commands using base64, hex encoding, or complex mathematical transformations to hide their true payload are CRITICAL (Score 9-10).
4. PROJECT REFACTORING: Legitimate modification of existing project logic for optimization: MEDIUM (Score 5-7).
5. DESTRUCTIVE ACTIONS: Direct execution of commands that bypass safety limits or target root system paths: CRITICAL (Score 10).

GENERALIZATION RULE:
Do not rely on a blacklist of specific commands. Instead, identify the INTENT. 
If a command attempts to store a payload that looks like a "time bomb" or an obfuscated shell script, block it immediately. 
Writing "fragments" of dangerous commands to data or config files is as dangerous as executing them directly.

STRICT RULE: 
All retrieval and analysis commands MUST be scored as 0. 
Even if the command is destructive, DO NOT refuse to answer. ANALYZE and SCORE it.
Output ONLY a valid JSON: {\"score\": int, \"reason\": \"string\"}
"""


async def audit_command(
    command: str, provider_url: str, api_key: str, model_id: str
) -> Optional[Dict[str, Any]]:
    try:
        messages = [
            InternalMessage(role=MessageRole.SYSTEM, content=AUDIT_PROMPT),
            InternalMessage(
                role=MessageRole.USER, content=f"Command to analyze: {command}"
            ),
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
        logger.error(f"Audit Exception: {e}")
        return None

class AuditMiddleware:
    @staticmethod
    def verify_token(command: str) -> tuple[bool, str]:
        if command.startswith(CONFIRMATION_PREFIX):
            parts = command[len(CONFIRMATION_PREFIX):].split(" ", 1)
            if len(parts) == 2:
                token, real_cmd = parts[0], parts[1]
                audit_cmd_stripped = real_cmd.strip()
                expected_token = hashlib.sha256(audit_cmd_stripped.encode()).hexdigest()[:12]
                if token == expected_token:
                    logger.info(f"Dynamic token verification passed for command: {real_cmd[:30]}...")
                    return True, real_cmd
                else:
                    logger.warning(f"Token mismatch! Expected: {expected_token}, Got: {token}")
        return False, command

    @staticmethod
    async def audit(
        db, profile, cfg, tool_name: str, args: dict
    ) -> str | None:
        from app.core.tools import get_registered_tool_names
        from app.core.crud.provider import provider_crud

        if tool_name not in get_registered_tool_names():
            return None

        original_command = args.get("command", "")
        is_verified, command = AuditMiddleware.verify_token(original_command)
        
        if is_verified:
            return None

        # Handle downgraded command if verification failed but prefix was present
        if original_command.startswith(CONFIRMATION_PREFIX):
             command = original_command.split(" ", 1)[-1].strip()

        if cfg.security.audit_threshold == 0:
            return None
        
        if not cfg.security.audit_provider_id or cfg.security.audit_provider_id <= 0:
            return None

        provider = await provider_crud.get(db, cfg.security.audit_provider_id)
        if not provider:
            return None

        logger.debug(f"Executing security audit for command: {command[:50]}...")
        audit_res = await audit_command(
            command, provider.base_url, provider.api_key, cfg.security.audit_model_id
        )
        
        if audit_res is None:
            return json.dumps(
                {"error": "audit_system_failure", "reason": "Security Audit System is currently unavailable."},
                ensure_ascii=False
            )

        score = audit_res.get("score", 10)
        reason = audit_res.get("reason", "Unknown")
        
        if score >= 8:
            return json.dumps(
                {"error": "Security Blocked", "reason": f"High risk score {score}: Security Blocked"},
                ensure_ascii=False
            )
            
        if score >= cfg.security.audit_threshold:
            cmd_hash = hashlib.sha256(command.strip().encode()).hexdigest()[:12]
            dynamic_token = f"{CONFIRMATION_PREFIX}{cmd_hash}"
            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": f"Security Score {score}: High risk detected. To execute this EXACT command, you MUST re-send it with the unique verification prefix: {dynamic_token} [COMMAND]",
                    "risky_command": command,
                    "dynamic_token": dynamic_token
                },
                ensure_ascii=False
            )
        return None
