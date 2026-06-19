from app.core import constants
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.schemas import RerankResult
from app.models.provider import ProviderType
from app.transformers.base import BaseRerankTransformer
from app.transformers.openai import OpenAITransformer

# 单个 chunk 发送给远程 reranker 前的最大字符截断长度，防止 Payload Too Large 及过大网络开销
RERANK_MAX_DOCUMENT_CHARS = 3000

logger = get_logger(__name__)


class RerankClient:
    _transformers: dict[str, BaseRerankTransformer] = {
        ProviderType.OPENAI.value.lower(): OpenAITransformer(),
    }

    @classmethod
    def get_transformer(cls, provider_type: ProviderType | str) -> BaseRerankTransformer:
        transformer = cls._transformers.get(str(provider_type).lower())
        if not transformer:
            raise LLMException(constants.ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL, detail=f"Unsupported rerank provider {provider_type}")
        return transformer

    @staticmethod
    def _truncate_documents(documents: list[str]) -> list[str]:
        # 强制截断单个 chunk 内容长度，保护远程接口
        return [(doc or "")[:RERANK_MAX_DOCUMENT_CHARS] for doc in documents]

    @classmethod
    async def rerank_texts(
        cls,
        provider_type: ProviderType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        timeout: float = 15.0,
    ) -> list[RerankResult]:
        # 空文档短路，避免无意义的远程调用
        if not documents:
            return []

        transformer = cls.get_transformer(provider_type)
        truncated_documents = cls._truncate_documents(documents)

        raw_results = await transformer.rerank_texts(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            query=query,
            documents=truncated_documents,
            top_n=top_n,
            timeout=timeout,
        )

        results = [RerankResult(index=item["index"], relevance_score=item["relevance_score"]) for item in raw_results]

        logger.bind(
            provider_type=str(provider_type),
            model_id=model_id,
            result_count=len(results),
            rerank_stage="response",
        ).info(t("LOG_RERANK_RESPONSE_FINISHED", count=len(results)))

        return results
