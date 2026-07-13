import json

from firecrawl import AsyncV1FirecrawlApp

from app.core import constants
from app.core.i18n import t
from app.core.utils.system import get_full_system_context

from .base import BaseExecutor


class FirecrawlSearchExecutor(BaseExecutor):
    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self._app = None

    def get_app(self):
        """
        获取 AsyncV1FirecrawlApp 实例。每次调用都新建实例，避免在并发环境下共享客户端状态。
        """
        api_key = self.cfg.tool.firecrawl_api_key if self.cfg else None
        return AsyncV1FirecrawlApp(api_key=api_key or "")

    async def execute(self, query: str) -> str:
        """
        使用 Firecrawl 搜索并直接返回 10 条默认搜索结果。

        :param query: 搜索查询词
        """
        system_info = get_full_system_context()

        # 显式检查 API Key
        api_key = self.cfg.tool.firecrawl_api_key if self.cfg else None

        if not api_key:
            return json.dumps({"error": t(constants.ERR_TOOL_FIRECRAWL_API_KEY_MISSING), "system_info": system_info}, ensure_ascii=False)

        try:
            self.logger.bind(uid=self.uid, query=query).info(t("LOG_FIRECRAWL_SEARCHING", limit=10, options={}))

            # Firecrawl SDK 提供原生异步 search，直接 await 便于任务取消时中断底层网络请求
            app = self.get_app()
            results = await app.search(
                query=query,
                limit=10,
            )

            # SDK 返回的是 Pydantic 模型，需要转换为字典才能 JSON 序列化
            results_dict = results.model_dump() if hasattr(results, "model_dump") else results
            return json.dumps(results_dict, ensure_ascii=False, default=str)

        except Exception as e:
            self.logger.bind(uid=self.uid, query=query).error(t("LOG_FIRECRAWL_SEARCH_FAILED"), exc_info=True)
            error_msg = str(e)
            if e is None or "NoneType" in error_msg:
                error_msg = t(constants.ERR_TOOL_FIRECRAWL_NETWORK_FAILED)
            elif "401" in error_msg or "Unauthorized" in error_msg:
                error_msg = t(constants.ERR_TOOL_FIRECRAWL_AUTH_FAILED)

            return json.dumps({"error": error_msg, "system_info": system_info}, ensure_ascii=False)


FIRECRAWL_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "firecrawl_search",
        "description": "Search the web using Firecrawl and return 10 results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
            },
            "required": ["query"],
        },
    },
}
