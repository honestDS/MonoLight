import asyncio
import json

from firecrawl import FirecrawlApp

from app.core.utils.system import get_full_system_context

from .base import BaseExecutor


class FirecrawlScrapeExecutor(BaseExecutor):
    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self._app = None

    def get_app(self):
        """
        获取 FirecrawlApp 实例。每次调用都新建实例，以避免在多线程池环境下共享同一个
        可能含有非线程安全状态的 SDK 实例。
        """
        api_key = self.cfg.tool.firecrawl_api_key if self.cfg else None
        return FirecrawlApp(api_key=api_key or "")

    async def execute(self, url: str, formats: list = ["markdown"], **kwargs) -> str:
        """
        使用 Firecrawl 解析网页。

        :param url: 目标网页 URL
        :param formats: 返回格式列表，可选 "markdown", "html", "raw_html", "screenshot", "links" 等
        :param kwargs: 其他高级抓取选项，如 mobile, wait_for, only_main_content 等
        """
        system_info = get_full_system_context()

        # 显式检查 API Key
        api_key = self.cfg.tool.firecrawl_api_key if self.cfg else None

        if not api_key:
            return json.dumps({"error": "Firecrawl API Key 未配置。请前往 [系统配置] -> [工具设置] -> [Firecrawl 配置] 中设置有效的 API Key。您可以从 https://www.firecrawl.dev/ 获取。", "system_info": system_info}, ensure_ascii=False)

        try:
            self.logger.bind(uid=self.uid, url=url).info(f"Firecrawl scraping: (formats={formats}, options={kwargs})")

            # Firecrawl SDK v2.x
            # 为了支持强制中断同步调用，我们需要在 executor 中运行
            app = self.get_app()
            doc = await self.run_sync(app.scrape_url, url, formats=formats, **kwargs)

            # SDK 返回的是 Pydantic 模型 (Document)，需要转换为字典才能 JSON 序列化
            doc_dict = doc.model_dump() if hasattr(doc, "model_dump") else doc

            return json.dumps({"status": "success", "url": url, "data": doc_dict, "system_info": system_info}, ensure_ascii=False)

        except Exception as e:
            self.logger.bind(uid=self.uid, url=url).error("Firecrawl scrape failed", exc_info=True)
            error_msg = str(e)
            if e is None or "NoneType" in error_msg:
                error_msg = "Firecrawl 服务响应异常（空响应）。请检查网络连接或 Firecrawl 服务状态。"
            elif "401" in error_msg or "Unauthorized" in error_msg:
                error_msg = "Firecrawl API Key 认证失败或已失效。请前往 [系统配置] -> [工具设置] -> [Firecrawl 配置] 检查并更新您的 API Key。"

            return json.dumps({"error": error_msg, "system_info": system_info}, ensure_ascii=False)


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
                "formats": {"type": "array", "items": {"type": "string", "enum": ["markdown", "html", "raw_html", "screenshot", "links"]}, "description": "The formats to return the content in.", "default": ["markdown"]},
                "only_main_content": {"type": "boolean", "description": "Only return the main content of the page. Excludes headers, footers, etc.", "default": True},
                "mobile": {"type": "boolean", "description": "Emulate a mobile device.", "default": False},
                "wait_for": {"type": "integer", "description": "Wait for a specified amount of time in milliseconds before scraping.", "default": 0},
            },
            "required": ["url"],
        },
    },
}
