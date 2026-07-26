from dataclasses import dataclass


@dataclass
class RerankResult:
    """远程 rerank 标准化结果：index 指向本次请求 documents 数组的下标"""

    index: int
    relevance_score: float


@dataclass
class RerankConfig:
    """从 Profile 解析得到的 rerank 运行配置"""

    channel_id: int
    channel_name: str | None
    api_key: str
    base_url: str
    model_id: str
    protocol: str
    candidate_k: int
    timeout: float
    priority: int
    http_proxy: str | None = None
