import json
import time

from firecrawl import AsyncV1FirecrawlApp
from firecrawl.v1.client import V1ScrapeOptions

from app.core import constants
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
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

    async def execute(self, query: str, limit: int = 5) -> str:
        """
        使用 Firecrawl 进行搜索，抓取格式固定为 Markdown。

        :param query: 搜索查询词
        :param limit: 返回结果数量限制
        """
        system_info = get_full_system_context()

        # 显式检查 API Key
        api_key = self.cfg.tool.firecrawl_api_key if self.cfg else None

        if not api_key:
            return json.dumps({"error": t(constants.ERR_TOOL_FIRECRAWL_API_KEY_MISSING), "system_info": system_info}, ensure_ascii=False)

        try:
            self.logger.bind(uid=self.uid, query=query).info(t("LOG_FIRECRAWL_SEARCHING", limit=limit, options={}))

            # 搜索结果固定抓取 Markdown，不向工具调用方开放抓取选项
            scrape_options = V1ScrapeOptions(formats=["markdown"])

            # Firecrawl SDK 提供原生异步 search，直接 await 便于任务取消时中断底层网络请求
            app = self.get_app()
            results = await app.search(
                query=query,
                limit=limit,
                scrape_options=scrape_options,
            )

            # SDK 返回的是 Pydantic 模型，需要转换为字典才能 JSON 序列化
            results_dict = results.model_dump() if hasattr(results, "model_dump") else results

            user_temp_dir = get_user_temp_dir(self.project_root, self.uid)
            user_temp_dir.mkdir(parents=True, exist_ok=True)
            timestamp_ms = time.time_ns() // 1_000_000
            result_filename = f"firecrawl_search_{timestamp_ms}.md"
            result_path = user_temp_dir / result_filename
            result_items = results_dict.get("data", []) if isinstance(results_dict, dict) else []
            markdown_content = "\n\n".join(
                str(item["markdown"])
                for item in result_items
                if isinstance(item, dict) and item.get("markdown")
            )
            await self.run_sync(result_path.write_text, markdown_content, encoding="utf-8")

            return json.dumps(
                {
                    "status": "success",
                    "result_file": str(result_path.resolve()),
                },
                ensure_ascii=False,
            )

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
        "description": "Search the web using Firecrawl. Search result pages are always returned as Markdown.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "limit": {"type": "integer", "description": "Maximum number of results to return.", "default": 5},
            },
            "required": ["query"],
        },
    },
}
