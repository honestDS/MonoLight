from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from firecrawl import AsyncFirecrawl
from firecrawl.v2.utils.error_handler import UnauthorizedError
from httpx import RequestError

from app.core.constants import (
    ERR_TOOL_FIRECRAWL_API_KEY_MISSING,
    ERR_TOOL_FIRECRAWL_AUTH_FAILED,
    ERR_TOOL_FIRECRAWL_NETWORK_FAILED,
)
from app.core.exceptions import ApiKeyException, ServerException
from app.core.i18n import t
from app.core.log import get_logger

if TYPE_CHECKING:
    from app.models.profile import ToolConfig


class FirecrawlWebClient:
    def __init__(self, api_key: str | None, uid: str = "default"):
        if not api_key:
            raise ApiKeyException(ERR_TOOL_FIRECRAWL_API_KEY_MISSING)

        self.api_key = api_key
        self.uid = uid
        self.logger = get_logger(self.__class__.__name__)

    async def search(self, query: str) -> Any:
        self.logger.bind(uid=self.uid, query=query).info(t("LOG_FIRECRAWL_SEARCHING", limit=10, options={}))
        return await self._request("search", query=query)

    async def scrape(
        self,
        url: str,
        formats: list[str],
        only_main_content: bool = True,
        mobile: bool = False,
        wait_for: int = 0,
    ) -> Any:
        options = {"only_main_content": only_main_content, "mobile": mobile, "wait_for": wait_for}
        self.logger.bind(uid=self.uid, url=url).info(t("LOG_FIRECRAWL_SCRAPING", formats=formats, options=options))
        return await self._request("scrape", url=url, formats=formats, **options)

    async def _request(self, operation: Literal["search", "scrape"], **params: Any) -> Any:
        try:
            app = AsyncFirecrawl(api_key=self.api_key)
            try:
                if operation == "search":
                    result = await app.search(query=params["query"], limit=10, timeout=30000)
                else:
                    result = await app.scrape(
                        params["url"],
                        formats=params["formats"],
                        only_main_content=params["only_main_content"],
                        mobile=params["mobile"],
                        wait_for=params["wait_for"],
                        timeout=30000,
                        max_age=0,
                        skip_tls_verification=False,
                    )
                return result.model_dump()
            finally:
                await app.v2.async_http_client.close()
        except Exception as exc:
            if operation == "search":
                self.logger.bind(uid=self.uid, query=params["query"]).error(t("LOG_FIRECRAWL_SEARCH_FAILED"), exc_info=True)
            else:
                self.logger.bind(uid=self.uid, url=params["url"]).error(t("LOG_FIRECRAWL_SCRAPE_FAILED"), exc_info=True)

            if isinstance(exc, RequestError):
                raise ServerException(ERR_TOOL_FIRECRAWL_NETWORK_FAILED) from exc
            if isinstance(exc, UnauthorizedError):
                raise ApiKeyException(ERR_TOOL_FIRECRAWL_AUTH_FAILED) from exc
            raise


def get_web_client(tool_config: ToolConfig | None, *, uid: str = "default") -> FirecrawlWebClient:
    """Return the backend-selected web client; service selection stays out of LLM tools."""
    api_key = tool_config.firecrawl_api_key if tool_config else None
    return FirecrawlWebClient(api_key=api_key, uid=uid)
