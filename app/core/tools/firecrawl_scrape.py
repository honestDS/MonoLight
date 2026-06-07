import json
from firecrawl import FirecrawlApp
from .base import BaseExecutor
from app.core.utils.system import get_full_system_context

class FirecrawlScrapeExecutor(BaseExecutor):
    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        # 实际使用中应该从配置或环境变量获取 API KEY
        self.app = FirecrawlApp(api_key="fc-da9f2b6f111d40809498e26d4e2ee8e8")

    async def execute(self, url: str, formats: list = ["markdown"], **kwargs) -> str:
        """
        使用 Firecrawl 解析网页。
        
        :param url: 目标网页 URL
        :param formats: 返回格式列表，可选 "markdown", "html", "raw_html", "screenshot", "links" 等
        :param kwargs: 其他高级抓取选项，如 mobile, wait_for, only_main_content 等
        """
        system_info = get_full_system_context()
        try:
            self.logger.info(f"[{self.uid}] Firecrawl scraping: {url} (formats={formats}, options={kwargs})")
            
            # Firecrawl SDK v2.x 
            doc = self.app.scrape_url(url, formats=formats, **kwargs)
            
            # SDK 返回的是 Pydantic 模型 (Document)，需要转换为字典才能 JSON 序列化
            doc_dict = doc.model_dump() if hasattr(doc, "model_dump") else doc

            return json.dumps({
                "status": "success",
                "url": url,
                "data": doc_dict,
                "system_info": system_info
            }, ensure_ascii=False)

        except Exception as e:
            self.logger.error(f"[{self.uid}] Firecrawl scrape failed: {e}")
            return json.dumps({
                "error": str(e),
                "system_info": system_info
            }, ensure_ascii=False)

FIRECRAWL_SCRAPE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "firecrawl_scrape",
        "description": "Scrape and parse a web page using Firecrawl with advanced options.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the web page to scrape.",
                },
                "formats": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["markdown", "html", "raw_html", "screenshot", "links"]
                    },
                    "description": "The formats to return the content in.",
                    "default": ["markdown"]
                },
                "only_main_content": {
                    "type": "boolean",
                    "description": "Only return the main content of the page. Excludes headers, footers, etc.",
                    "default": True
                },
                "mobile": {
                    "type": "boolean",
                    "description": "Emulate a mobile device.",
                    "default": False
                },
                "wait_for": {
                    "type": "integer",
                    "description": "Wait for a specified amount of time in milliseconds before scraping.",
                    "default": 0
                }
            },
            "required": ["url"],
        },
    },
}
