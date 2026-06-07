import json
from firecrawl import FirecrawlApp
from .base import BaseExecutor
from app.core.utils.system import get_full_system_context

class FirecrawlSearchExecutor(BaseExecutor):
    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        # 实际使用中应该从配置或环境变量获取 API KEY
        self.app = FirecrawlApp(api_key="fc-da9f2b6f111d40809498e26d4e2ee8e8")

    async def execute(self, query: str, limit: int = 5, **kwargs) -> str:
        """
        使用 Firecrawl 进行搜索。
        
        :param query: 搜索查询词
        :param limit: 返回结果数量限制
        :param kwargs: 其他搜索选项，如 scrape_options 等
        """
        system_info = get_full_system_context()
        try:
            self.logger.info(f"[{self.uid}] Firecrawl searching: {query} (limit={limit}, options={kwargs})")
            
            # 兼容处理：将 scrapeOptions 转换为 scrape_options
            if "scrapeOptions" in kwargs:
                kwargs["scrape_options"] = kwargs.pop("scrapeOptions")

            # 默认使用 markdown 格式进行抓取
            if "scrape_options" not in kwargs:
                kwargs["scrape_options"] = {"formats": ["markdown"]}
            elif isinstance(kwargs["scrape_options"], dict) and "formats" not in kwargs["scrape_options"]:
                kwargs["scrape_options"]["formats"] = ["markdown"]

            # Firecrawl SDK v2.x search 参数是命名的
            results = self.app.search(query=query, limit=limit, **kwargs)
            
            # SDK 返回的是 Pydantic 模型，需要转换为字典才能 JSON 序列化
            results_dict = results.model_dump() if hasattr(results, "model_dump") else results

            return json.dumps({
                "status": "success",
                "query": query,
                "results": results_dict,
                "system_info": system_info
            }, ensure_ascii=False)

        except Exception as e:
            self.logger.error(f"[{self.uid}] Firecrawl search failed: {e}")
            return json.dumps({
                "error": str(e),
                "system_info": system_info
            }, ensure_ascii=False)

FIRECRAWL_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "firecrawl_search",
        "description": "Search the web using Firecrawl with advanced options.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 5
                },
                "location": {
                    "type": "string",
                    "description": "The location to use for the search.",
                },
                "scrape_options": {
                    "type": "object",
                    "description": "Options for scraping the search results, such as formats.",
                    "default": {"formats": ["markdown"]},
                    "properties": {
                        "formats": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["markdown", "html", "raw_html", "screenshot", "links"]
                            },
                            "default": ["markdown"]
                        }
                    }
                }
            },
            "required": ["query"],
        },
    },
}
