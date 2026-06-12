import os

import tiktoken
from dotenv import load_dotenv

load_dotenv()


def estimate_tokens(text: str) -> int:
    """
    估算文本的 Token 数量。使用 tiktoken 进行更准确的估算，如果没有安装则回退到字符估算。
    默认使用 gpt-3.5-turbo 的编码方式 (cl100k_base)。
    """
    if not text:
        return 0

    try:
        # 使用 tiktoken 计算更准确的 token 数量 (基于 OpenAI 的 cl100k_base)
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text, disallowed_special=()))
    except Exception:
        # 降级：如果 tiktoken 解析失败或未安装，使用更符合实际的预估值
        # 一个汉字通常占 1~2 个 Token，一个英文单词通常占 1.3 个 Token（英文字母约为 0.25~0.3）
        chinese_count = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
        other_count = len(text) - chinese_count
        c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
        o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
        return int(chinese_count * c_coeff + other_count * o_coeff)
