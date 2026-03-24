import os
from dotenv import load_dotenv

load_dotenv()


def estimate_tokens(text: str) -> int:
    """
    估算文本的 Token 数量。基于中文字符与非中文字符的加权计算。
    """
    if not text:
        return 0
    chinese_count = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
    other_count = len(text) - chinese_count
    c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 0.6))
    o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    return int(chinese_count * c_coeff + other_count * o_coeff)
