from dataclasses import dataclass

from app.models.provider import ProviderType


@dataclass
class RerankResult:
    """远程 rerank 标准化结果：index 指向本次请求 documents 数组的下标"""

    index: int
    relevance_score: float


@dataclass
class RerankConfig:
    """从 Profile 解析得到的 rerank 运行配置"""

    provider_id: int
    provider_name: str | None
    provider_type: ProviderType
    api_key: str
    base_url: str
    model_id: str
    candidate_k: int
    timeout: float
    priority: int
