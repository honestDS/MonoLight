import json

from app.core.embedding.knowledge_base import KNOWLEDGE_BASE_QUERY_TOP_K, query_knowledge_base
from app.core.tools.base import BaseExecutor

KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_knowledge_base",
        "description": "Query an available knowledge base by semantic similarity. Use this when the answer may require facts from uploaded knowledge base documents.",
        "parameters": {
            "type": "object",
            "properties": {"knowledge_base_id": {"type": "integer", "description": "The id of an allowed knowledge base. Must be one of the ids allowed by the current runtime whitelist."}, "query": {"type": "string", "description": "The semantic search query."}},
            "required": ["knowledge_base_id", "query"],
        },
    },
}


class KnowledgeBaseQueryExecutor(BaseExecutor):
    async def execute(self, **kwargs) -> str:
        kb_id = kwargs.get("knowledge_base_id")
        query = kwargs.get("query")

        if kb_id is None or query is None:
            return json.dumps({"error": "Missing required arguments: knowledge_base_id and query"}, ensure_ascii=False)

        try:
            kb_id = int(kb_id)
        except (TypeError, ValueError):
            return json.dumps({"error": f"Invalid knowledge_base_id: {kb_id}. It must be an integer."}, ensure_ascii=False)

        # 校验白名单
        if not self.allowed_knowledge_base_ids or kb_id not in self.allowed_knowledge_base_ids:
            return json.dumps({"error": f"Unauthorized knowledge_base_id: {kb_id}. It is not in the whitelist of allowed knowledge bases."}, ensure_ascii=False)

        if not self.db or not self.profile:
            return json.dumps({"error": "Database session or Profile configuration is missing in runtime context."}, ensure_ascii=False)

        try:
            # 严格使用内部固定 KNOWLEDGE_BASE_QUERY_TOP_K 检索
            response_data = await query_knowledge_base(
                db=self.db,
                profile=self.profile,
                kb_id=kb_id,
                query=query,
                top_k=KNOWLEDGE_BASE_QUERY_TOP_K,
            )
            # 组装返回格式
            items = []
            for item in response_data.items:
                metadata = item.metadata_ or {}
                items.append(
                    {
                        "source": metadata.get("filename") or "未知来源",
                        "content": item.content,
                    }
                )
            return json.dumps({"items": items}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Failed to query knowledge base: {str(e)}"}, ensure_ascii=False)
