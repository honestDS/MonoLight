from typing import Any, Dict, List
from .base import BaseTransformer

class OpenAITransformer(BaseTransformer):
    @classmethod
    def to_standard(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        return data

    @classmethod
    def from_standard(cls, internal_data: Dict[str, Any]) -> Dict[str, Any]:
        # 尝试从 OpenAI 嵌套结构中提取内容
        choices = internal_data.get('choices', [])
        content = ''
        if choices and len(choices) > 0:
            message = choices[0].get('message', {})
            content = message.get('content', '')
        
        # 如果顶级目录直接有 content (兼容性处理)
        if not content:
            content = internal_data.get('content', '')

        return {
            'id': internal_data.get('id', ''),
            'object': 'chat.completion',
            'created': internal_data.get('created', 0),
            'model': internal_data.get('model', ''),
            'choices': [
                {
                    'index': 0,
                    'message': {
                        'role': 'assistant',
                        'content': content,
                    },
                    'finish_reason': 'stop'
                }
            ],
            'usage': internal_data.get('usage', {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0
            })
        }

class AnthropicTransformer(BaseTransformer):
    @classmethod
    def from_standard(cls, internal_data: Dict[str, Any]) -> Dict[str, Any]:
        return {'status': 'placeholder', 'msg': 'Anthropic format not implemented'}

class GoogleTransformer(BaseTransformer):
    @classmethod
    def from_standard(cls, internal_data: Dict[str, Any]) -> Dict[str, Any]:
        return {'status': 'placeholder', 'msg': 'Google Gemini format not implemented'}