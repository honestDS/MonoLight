import json
import aiohttp
from typing import Dict, Any, Optional
from app.core.log import get_logger

logger = get_logger(__name__)

AUDIT_PROMPT = """
You are a security expert. Analyze the following shell command or Python code for potential security risks.
Rate the risk from 0 (Safe) to 10 (Critical/Destructive).
Output ONLY a valid JSON: {"score": int, "reason": "string"}
"""

async def audit_command(command: str, provider_url: str, api_key: str, model_id: str) -> Optional[Dict[str, Any]]:
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": AUDIT_PROMPT},
                {"role": "user", "content": command}
            ],
            "response_format": {"type": "json_object"}
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(provider_url, headers=headers, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Audit API error: {resp.status}")
                    return None
                result = await resp.json()
                content = result["choices"][0]["message"]["content"]
                return json.loads(content)
    except Exception as e:
        logger.error(f"Audit Exception: {e}")
        return None
