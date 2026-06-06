"""
Embedding 工具函数

提供向量相似度计算、归一化等常用功能。
"""

import math


def normalize_vector(vector: list[float]) -> list[float]:
    """
    向量归一化（L2 范数）

    Args:
        vector: 原始向量

    Returns:
        List[float]: 归一化后的向量
    """
    magnitude = math.sqrt(sum(x * x for x in vector))
    if magnitude == 0:
        return vector
    return [x / magnitude for x in vector]


def cosine_similarity(vector1: list[float], vector2: list[float]) -> float:
    """
    计算两个向量的余弦相似度

    Args:
        vector1: 第一个向量
        vector2: 第二个向量

    Returns:
        float: 余弦相似度，范围 [-1, 1]

    Raises:
        ValueError: 当向量维度不匹配时
    """
    if len(vector1) != len(vector2):
        raise ValueError(f"向量维度不匹配: {len(vector1)} vs {len(vector2)}")

    if len(vector1) == 0:
        return 0.0

    dot_product = sum(a * b for a, b in zip(vector1, vector2))
    magnitude1 = math.sqrt(sum(x * x for x in vector1))
    magnitude2 = math.sqrt(sum(x * x for x in vector2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


def batch_similarity(query_vector: list[float], vectors: list[list[float]]) -> list[tuple[int, float]]:
    """
    计算查询向量与多个向量的相似度

    Args:
        query_vector: 查询向量
        vectors: 目标向量列表

    Returns:
        List[Tuple[int, float]]: (索引, 相似度) 的列表，按相似度降序排列
    """
    similarities = []
    for idx, vector in enumerate(vectors):
        sim = cosine_similarity(query_vector, vector)
        similarities.append((idx, sim))

    similarities.sort(key=lambda x: x[1], reverse=True)
    return similarities


def euclidean_distance(vector1: list[float], vector2: list[float]) -> float:
    """
    计算两个向量的欧氏距离

    Args:
        vector1: 第一个向量
        vector2: 第二个向量

    Returns:
        float: 欧氏距离

    Raises:
        ValueError: 当向量维度不匹配时
    """
    if len(vector1) != len(vector2):
        raise ValueError(f"向量维度不匹配: {len(vector1)} vs {len(vector2)}")

    return math.sqrt(sum((a - b) ** 2 for a, b in zip(vector1, vector2)))


def dot_product(vector1: list[float], vector2: list[float]) -> float:
    """
    计算两个向量的点积

    Args:
        vector1: 第一个向量
        vector2: 第二个向量

    Returns:
        float: 点积值

    Raises:
        ValueError: 当向量维度不匹配时
    """
    if len(vector1) != len(vector2):
        raise ValueError(f"向量维度不匹配: {len(vector1)} vs {len(vector2)}")

    return sum(a * b for a, b in zip(vector1, vector2))
