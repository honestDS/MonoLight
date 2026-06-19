"""渠道模型：渠道规则与渠道配置"""

from pydantic import (
    BaseModel,
)
from pydantic import (
    Field as PydanticField,
)


class ChannelRule(BaseModel):
    """单条渠道路由规则"""

    provider_id: int = PydanticField(..., gt=0, description="提供商 ID")
    model_id: str = PydanticField(..., min_length=1, description="模型标识符")
    priority: int = PydanticField(..., ge=1, description="优先级分组，越小越优先；同组内失败会降级到下一组")
    weight: int = PydanticField(..., ge=0, description="同优先级组内的轮询配额：一个轮询周期内该渠道被使用的次数")


class ChannelConfig(BaseModel):
    """渠道配置（按用途独立）"""

    chat_timeout: float = PydanticField(60.0, gt=0, le=600, description="对话渠道调用超时（秒）")
    embedding_timeout: float = PydanticField(30.0, gt=0, le=600, description="嵌入渠道调用超时（秒）")
    rerank_timeout: float = PydanticField(15.0, gt=0, le=120, description="重排渠道调用超时（秒）")
    rerank_candidate_k: int = PydanticField(20, gt=0, le=50, description="送入远程 reranker 的候选数量")
    kb_query_top_k: int = PydanticField(5, gt=0, le=50, description="知识库检索最终返回的片段数量")
    rules: list[ChannelRule] = PydanticField(default_factory=list, description="路由规则列表")
