import re

import jieba

_WORD_PATTERN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_ASCII_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def tokenize_for_sparse_search(text: str) -> list[str]:
    if not text:
        return []

    tokens: list[str] = []

    for token in _ASCII_WORD_PATTERN.findall(text):
        normalized = token.strip().lower()
        if normalized:
            tokens.append(normalized)

    chinese_text = _ASCII_WORD_PATTERN.sub(" ", text)
    for token in jieba.lcut(chinese_text):
        normalized = token.strip().lower()
        if not normalized:
            continue
        if _WORD_PATTERN.search(normalized):
            tokens.append(normalized)

    return tokens
