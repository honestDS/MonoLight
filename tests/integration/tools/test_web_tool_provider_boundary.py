import asyncio
import json

import httpx
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


@pytest.fixture
def firecrawl_http(monkeypatch):
    state = {
        "status_code": 200,
        "response_json": {},
        "requests": [],
        "clients": [],
        "system_info": {"runtime": "fixed test context"},
        "request_started": asyncio.Event(),
        "response_ready": asyncio.Event(),
    }
    state["response_ready"].set()
    original_async_client = httpx.AsyncClient

    async def _handle_request(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        state["request_started"].set()
        await state["response_ready"].wait()
        return httpx.Response(
            state["status_code"],
            json=state["response_json"],
            request=request,
        )

    transport = httpx.MockTransport(_handle_request)

    def _async_client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        client = original_async_client(*args, **kwargs)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", _async_client_factory)
    monkeypatch.setattr(web_tool_module, "get_full_system_context", lambda: state["system_info"])

    yield state

    assert state["clients"]
    assert all(client.is_closed for client in state["clients"])


def _configured_firecrawl_executor() -> WebToolExecutor:
    cfg = ProfileConfig.model_validate({"tool": {"firecrawl_api_key": "firecrawl-test-key", "enabled_tools": [WEB_TOOL_NAME]}})
    executor = WebToolExecutor(project_root=".", uid="user-1")
    executor.set_config(cfg)
    return executor


@pytest.mark.asyncio
async def test_web_tool_search_uses_firecrawl_v2_http_protocol(firecrawl_http):
    firecrawl_http["response_json"] = {
        "success": True,
        "data": {
            "web": [
                {
                    "url": "https://search.example.test/result",
                    "title": "Search result",
                    "description": "Search description",
                }
            ]
        },
    }
    query = "v2 offline search"

    result = json.loads(await _configured_firecrawl_executor().execute(operation="search", query=query))

    assert len(firecrawl_http["requests"]) == 1
    request = firecrawl_http["requests"][0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.firecrawl.dev/v2/search"
    assert request.headers["Authorization"] == "Bearer firecrawl-test-key"
    request_body = json.loads(request.content)
    assert request_body == {
        "query": query,
        "limit": 10,
        "timeout": 30000,
        "origin": "python-sdk@4.28.2",
    }
    assert result == {
        "web": [
            {
                "url": "https://search.example.test/result",
                "title": "Search result",
                "description": "Search description",
                "category": None,
            }
        ],
        "news": None,
        "images": None,
    }
    assert "data" not in result
    assert "count" not in result


@pytest.mark.asyncio
async def test_web_tool_default_scrape_uses_firecrawl_v2_http_protocol(firecrawl_http):
    firecrawl_http["response_json"] = {
        "success": True,
        "data": {"markdown": "# Offline page"},
    }
    url = "https://scrape.example.test/article"

    result = json.loads(await _configured_firecrawl_executor().execute(operation="scrape", url=url))

    assert len(firecrawl_http["requests"]) == 1
    request = firecrawl_http["requests"][0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.firecrawl.dev/v2/scrape"
    request_body = json.loads(request.content)
    assert request_body == {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "mobile": False,
        "waitFor": 0,
        "timeout": 30000,
        "maxAge": 0,
        "skipTlsVerification": False,
        "removeBase64Images": True,
        "fastMode": False,
        "blockAds": True,
        "storeInCache": True,
        "origin": "python-sdk@4.28.2",
    }
    assert result["status"] == "success"
    assert result["url"] == url
    assert result["data"]["markdown"] == "# Offline page"
    assert result["system_info"] == firecrawl_http["system_info"]


@pytest.mark.asyncio
async def test_web_tool_multi_format_scrape_maps_firecrawl_v2_fields(firecrawl_http):
    firecrawl_http["response_json"] = {
        "success": True,
        "data": {
            "rawHtml": "<main>Offline page</main>",
            "html": "<html><main>Offline page</main></html>",
            "screenshot": "base64-screenshot",
            "links": ["https://scrape.example.test/next"],
            "metadata": {
                "sourceURL": "https://scrape.example.test/article",
                "statusCode": 200,
            },
        },
    }
    url = "https://scrape.example.test/article"
    scrape_options = {
        "url": url,
        "formats": ["raw_html", "html", "screenshot", "links"],
        "only_main_content": False,
        "mobile": True,
        "wait_for": 1250,
    }

    result = json.loads(await _configured_firecrawl_executor().execute(operation="scrape", **scrape_options))

    assert len(firecrawl_http["requests"]) == 1
    request = firecrawl_http["requests"][0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.firecrawl.dev/v2/scrape"
    request_body = json.loads(request.content)
    assert request_body == {
        "url": url,
        "formats": ["rawHtml", "html", "screenshot", "links"],
        "onlyMainContent": False,
        "mobile": True,
        "waitFor": 1250,
        "timeout": 30000,
        "maxAge": 0,
        "skipTlsVerification": False,
        "removeBase64Images": True,
        "fastMode": False,
        "blockAds": True,
        "storeInCache": True,
        "origin": "python-sdk@4.28.2",
    }
    assert result["status"] == "success"
    assert result["url"] == url
    assert result["data"]["raw_html"] == "<main>Offline page</main>"
    assert result["data"]["html"] == "<html><main>Offline page</main></html>"
    assert result["data"]["screenshot"] == "base64-screenshot"
    assert result["data"]["links"] == ["https://scrape.example.test/next"]
    assert result["data"]["metadata"]["source_url"] == url
    assert result["data"]["metadata"]["status_code"] == 200
    assert result["system_info"] == firecrawl_http["system_info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("search", {"query": "cancelled search"}),
        ("scrape", {"url": "https://scrape.example.test/cancelled"}),
    ],
)
async def test_web_tool_cancellation_closes_firecrawl_client(operation, arguments, firecrawl_http):
    firecrawl_http["response_ready"].clear()
    task = asyncio.create_task(_configured_firecrawl_executor().execute(operation=operation, **arguments))

    await asyncio.wait_for(firecrawl_http["request_started"].wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("search", {"query": "unauthorized search"}),
        ("scrape", {"url": "https://scrape.example.test/unauthorized"}),
    ],
)
@pytest.mark.parametrize(
    ("locale", "expected_error"),
    [
        ("zh", "Firecrawl API Key 认证失败或已失效，请检查并更新配置"),
        ("en", "Firecrawl API key authentication failed or expired. Please update the configuration"),
    ],
)
async def test_web_tool_localizes_firecrawl_unauthorized_errors(
    firecrawl_http,
    operation,
    arguments,
    locale,
    expected_error,
):
    firecrawl_http["status_code"] = 401
    firecrawl_http["response_json"] = {"success": False, "error": "invalid api key"}
    token = set_current_locale(locale)
    try:
        result = json.loads(await _configured_firecrawl_executor().execute(operation=operation, **arguments))
    finally:
        reset_current_locale(token)

    assert result["error"] == expected_error
    assert result["system_info"] == firecrawl_http["system_info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("search", {"query": "rate limited search"}),
        ("scrape", {"url": "https://scrape.example.test/rate-limited"}),
    ],
)
async def test_web_tool_keeps_firecrawl_rate_limit_errors(operation, arguments, firecrawl_http):
    firecrawl_http["status_code"] = 429
    firecrawl_http["response_json"] = {"success": False, "error": "too many requests"}

    result = json.loads(await _configured_firecrawl_executor().execute(operation=operation, **arguments))

    assert "Rate Limit Exceeded" in result["error"]
    assert "Unauthorized" not in result["error"]
    assert "status" not in result
    assert result["system_info"] == firecrawl_http["system_info"]
