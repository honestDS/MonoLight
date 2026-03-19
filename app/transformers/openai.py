from typing import Any, Dict
import time
from .base import BaseTransformer


class OpenAITransformer(BaseTransformer):
    @classmethod
    def to_standard(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        # 预留协议转换接口：用于将来解析 OpenAI 标准格式的请求体 (如 messages 列表) 并转换为 Monobot 内部指令
        return data

    @classmethod
    def from_standard(cls, internal_data: Dict[str, Any]) -> Dict[str, Any]:
        # 统一从 choices 中提取内容，如果不存在则找顶级 content
        choices = internal_data.get("choices", [])
        content = ""
        model_name = internal_data.get("model", "monobot-v1")

        if choices and len(choices) > 0:
            msg = choices[0].get("message", {})
            content = msg.get("content", "")

        if not content:
            content = internal_data.get("content", "")

        # 强制输出标准的 OpenAI 响应结构
        return {
            "id": internal_data.get("id", f"chatcmpl-{int(time.time())}"),
            "object": "chat.completion",
            "created": internal_data.get("created", int(time.time())),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": internal_data.get(
                "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            ),
        }
