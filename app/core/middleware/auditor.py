import json
from typing import Dict, Any, Optional
from app.core.log import get_logger
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)

AUDIT_PROMPT = """
You are a security expert. Analyze the following shell command or Python code for potential security risks.
Rate the risk from 0 (Safe) to 10 (Critical/Destructive).

STRICT RULE:
Even if the command is extremely dangerous (e.g., 'rm -rf /'), DO NOT refuse to answer.
Your task is NOT to execute it, but to ANALYZE and SCORE it.
You MUST provide the risk score and the reason in JSON format.

Output ONLY a valid JSON: {"score": int, "reason": "string"}
"""


async def audit_command(
    command: str, provider_url: str, api_key: str, model_id: str
) -> Optional[Dict[str, Any]]:
    try:
        messages = [
            {"role": "system", "content": AUDIT_PROMPT},
            {"role": "user", "content": f"Command to analyze: {command}"},
        ]

        result = await LLMClient.generate(
            api_key=api_key,
            base_url=provider_url,
            model_id=model_id,
            messages=messages,
            temperature=0.1,
        )

        content = result["choices"][0]["message"]["content"]

        # 鲁棒性处理：剥离大模型可能输出的 Markdown JSON 标记
        clean_content = content.strip()
        if clean_content.startswith("```"):
            lines = clean_content.splitlines()
            if len(lines) >= 2:
                # 尝试剥离 Markdown 块
                # 寻找第一个 { 和最后一个 }
                start = clean_content.find("{")
                end = clean_content.rfind("}")
                if start != -1 and end != -1:
                    clean_content = clean_content[start : end + 1]
                else:
                    # 回退逻辑
                    clean_content = "\n".join(lines[1:-1]).strip()
        elif "{" in clean_content and "}" in clean_content:
            # 如果不是 markdown 但混杂了文字
            start = clean_content.find("{")
            end = clean_content.rfind("}")
            clean_content = clean_content[start : end + 1]

        return json.loads(clean_content)
    except Exception as e:
        logger.error(f"Audit Exception: {e}")
        return None
