import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.profile import get_profile_tool_options
from app.core.constants import WEB_TOOL_NAME
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.tools import get_tools_for_profile
from app.core.tools import web_tool as web_tool_module
from app.core.tools.web_tool import WebToolExecutor
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round
from app.models.message import InternalToolCall
from app.models.profile import Profile, ProfileConfig


class _InternalWebClient:
    def __init__(self) -> None:
        self.search_queries: list[str] = []
        self.scrape_requests: list[dict[str, object]] = []

    async def search(self, query: str) -> dict[str, object]:
        self.search_queries.append(query)
        return {
            "service": "internal-pages",
            "query": query,
            "results": [{"title": "Internal result", "url": "https://pages.example.test/result"}],
        }

    async def scrape(
        self,
        url: str,
        formats: list[str],
        only_main_content: bool,
        mobile: bool,
        wait_for: int,
    ) -> dict[str, object]:
        self.scrape_requests.append(
            {
                "url": url,
                "formats": formats,
                "only_main_content": only_main_content,
                "mobile": mobile,
                "wait_for": wait_for,
            }
        )
        return {"service": "internal-pages", "url": url, "formats": formats, "content": "raw page"}


@pytest.mark.asyncio
async def test_web_tool_uses_an_alternate_client_without_firecrawl_configuration(monkeypatch):
    cfg = ProfileConfig.model_validate({"tool": {"firecrawl_api_key": "", "enabled_tools": [WEB_TOOL_NAME]}})
    client = _InternalWebClient()
    system_info = {"runtime": "fixed test context"}

    def get_internal_client(tool_config, *, uid: str):
        assert tool_config.firecrawl_api_key == ""
        assert uid == "user-1"
        return client

    monkeypatch.setattr(web_tool_module, "get_web_client", get_internal_client)
    monkeypatch.setattr(web_tool_module, "get_full_system_context", lambda: system_info)

    executor = WebToolExecutor(project_root=".", uid="user-1")
    executor.set_config(cfg)

    query = "internal service query"
    search_result = await executor.execute(operation="search", query=query)
    assert json.loads(search_result) == {
        "service": "internal-pages",
        "query": query,
        "results": [{"title": "Internal result", "url": "https://pages.example.test/result"}],
    }
    assert client.search_queries == [query]

    url = "https://pages.example.test/article"
    scrape_options = {
        "url": url,
        "formats": ["raw_html", "html"],
        "only_main_content": False,
        "mobile": True,
        "wait_for": 1250,
    }
    scrape_result = await executor.execute(operation="scrape", **scrape_options)
    assert client.scrape_requests == [scrape_options]
    assert json.loads(scrape_result) == {
        "status": "success",
        "url": url,
        "data": {
            "service": "internal-pages",
            "url": url,
            "formats": ["raw_html", "html"],
            "content": "raw page",
        },
        "system_info": system_info,
    }


@pytest.mark.asyncio
async def test_profile_exposes_one_web_tool_protocol_and_rejects_provider_arguments(
    db_session: AsyncSession,
):
    cfg = ProfileConfig.model_validate({"tool": {"firecrawl_api_key": "", "enabled_tools": [WEB_TOOL_NAME]}})
    profile = Profile(
        id=None,
        uid="user-1",
        name="web-tool-profile",
        configs=cfg.model_dump(),
    )

    tools, whitelist = await get_tools_for_profile(db_session, profile)
    tool_names = {tool["function"]["name"] for tool in tools}
    web_schemas = [tool for tool in tools if tool["function"]["name"] == WEB_TOOL_NAME]

    assert len(web_schemas) == 1
    assert tool_names.isdisjoint({"firecrawl_search", "firecrawl_scrape", "search", "scrape"})
    assert whitelist == []
    web_schema = web_schemas[0]
    assert web_schema["function"]["parameters"]["properties"]["operation"]["enum"] == ["search", "scrape"]

    valid_calls = [
        InternalToolCall(
            id="search-call",
            name=WEB_TOOL_NAME,
            arguments={"operation": "search", "query": "internal service query"},
        ),
        InternalToolCall(
            id="scrape-call",
            name=WEB_TOOL_NAME,
            arguments={"operation": "scrape", "url": "https://pages.example.test/article", "formats": ["raw_html"]},
        ),
    ]
    assert prevalidate_tool_round(valid_calls, cfg, tool_schemas=tools) == {}

    invalid_call = InternalToolCall(
        id="provider-call",
        name=WEB_TOOL_NAME,
        arguments={"operation": "search", "query": "internal service query", "provider": "internal-pages"},
    )
    errors = prevalidate_tool_round([invalid_call], cfg, tool_schemas=tools)
    assert json.loads(errors[invalid_call.id])["unsupported_arguments"] == ["provider"]


@pytest.mark.parametrize(
    ("locale", "expected_label"),
    [("zh", "网页搜索与抓取"), ("en", "Web Search and Scrape")],
)
def test_profile_tool_options_localize_the_single_web_tool(locale: str, expected_label: str):
    token = set_current_locale(locale)
    try:
        options = get_profile_tool_options()
    finally:
        reset_current_locale(token)

    web_options = [option for option in options if option["value"] == WEB_TOOL_NAME]
    assert web_options == [{"value": WEB_TOOL_NAME, "label": expected_label}]
