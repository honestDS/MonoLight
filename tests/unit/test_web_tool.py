import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.tools import web_tool as web_tool_module
from app.core.tools.web_tool import WebToolExecutor
from app.models.profile import ProfileConfig
from app.providers import web as web_provider_module

API_KEY = "test-firecrawl-key"
SYSTEM_INFO = {"runtime": "test"}
URL = "https://example.test/page"


@pytest.fixture
def firecrawl_dependencies(monkeypatch):
    sdk = SimpleNamespace(search=AsyncMock(), scrape_url=AsyncMock())
    app_factory = Mock(return_value=sdk)
    system_info = Mock(return_value=SYSTEM_INFO)
    monkeypatch.setattr(web_provider_module, "AsyncV1FirecrawlApp", app_factory)
    monkeypatch.setattr(web_tool_module, "get_full_system_context", system_info)
    return SimpleNamespace(sdk=sdk, app_factory=app_factory, system_info=system_info)


@pytest.fixture
def executor():
    instance = WebToolExecutor(project_root=".", uid="user-1")
    instance.set_config(ProfileConfig.model_validate({"tool": {"firecrawl_api_key": API_KEY}}))
    return instance


@pytest.fixture
def current_locale(request):
    token = set_current_locale(request.param)
    try:
        yield request.param
    finally:
        reset_current_locale(token)


@pytest.mark.asyncio
async def test_search_uses_fixed_limit_and_returns_sdk_model_dump(executor, firecrawl_dependencies):
    result_data = {"data": [{"title": "Example", "url": URL}], "count": 1}
    sdk_result = Mock()
    sdk_result.model_dump.return_value = result_data
    firecrawl_dependencies.sdk.search.return_value = sdk_result

    result = await executor.execute(operation="search", query="example query")

    assert json.loads(result) == result_data
    firecrawl_dependencies.app_factory.assert_called_once_with(api_key=API_KEY)
    firecrawl_dependencies.sdk.search.assert_awaited_once_with(query="example query", limit=10)
    sdk_result.model_dump.assert_called_once_with()
    firecrawl_dependencies.system_info.assert_called_once_with()


@pytest.mark.asyncio
async def test_scrape_uses_markdown_and_option_defaults_and_returns_sdk_model_dump(executor, firecrawl_dependencies):
    result_data = {"markdown": "Page text"}
    sdk_result = Mock()
    sdk_result.model_dump.return_value = result_data
    firecrawl_dependencies.sdk.scrape_url.return_value = sdk_result

    result = await executor.execute(operation="scrape", url=URL)

    assert json.loads(result) == {
        "status": "success",
        "url": URL,
        "data": result_data,
        "system_info": SYSTEM_INFO,
    }
    firecrawl_dependencies.sdk.scrape_url.assert_awaited_once_with(
        URL,
        formats=["markdown"],
        only_main_content=True,
        mobile=False,
        wait_for=0,
    )
    sdk_result.model_dump.assert_called_once_with()


@pytest.mark.asyncio
async def test_scrape_converts_raw_html_and_forwards_all_options(executor, firecrawl_dependencies):
    firecrawl_dependencies.sdk.scrape_url.return_value = {"html": "<main>Page</main>"}

    result = await executor.execute(
        operation="scrape",
        url=URL,
        formats=["raw_html", "html", "screenshot", "links"],
        only_main_content=False,
        mobile=True,
        wait_for=1250,
    )

    assert json.loads(result) == {
        "status": "success",
        "url": URL,
        "data": {"html": "<main>Page</main>"},
        "system_info": SYSTEM_INFO,
    }
    firecrawl_dependencies.sdk.scrape_url.assert_awaited_once_with(
        URL,
        formats=["rawHtml", "html", "screenshot", "links"],
        only_main_content=False,
        mobile=True,
        wait_for=1250,
    )


@pytest.mark.asyncio
async def test_default_scrape_formats_do_not_leak_between_calls(executor, firecrawl_dependencies):
    firecrawl_dependencies.sdk.scrape_url.return_value = {}

    await executor.execute(operation="scrape", url=URL)
    first_formats = firecrawl_dependencies.sdk.scrape_url.await_args_list[0].kwargs["formats"]
    first_formats.append("html")
    await executor.execute(operation="scrape", url=URL)
    second_formats = firecrawl_dependencies.sdk.scrape_url.await_args_list[1].kwargs["formats"]

    assert first_formats == ["markdown", "html"]
    assert second_formats == ["markdown"]
    assert second_formats is not first_formats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments", "expected_detail"),
    [
        ("search", {}, "query"),
        ("scrape", {}, "url"),
        ("crawl", {}, "allowed_operations"),
        ("search", {"query": "topic", "url": URL}, "url"),
        ("scrape", {"url": URL, "query": "topic"}, "query"),
        ("search", {"query": 42}, "query"),
        ("scrape", {"url": URL, "mobile": "true"}, "mobile"),
        ("scrape", {"url": URL, "formats": ["xml"]}, "formats"),
        ("scrape", {"url": URL, "wait_for": -1}, "wait_for"),
        ("scrape", {"url": URL, "wait_for": True}, "wait_for"),
    ],
    ids=[
        "missing-query",
        "missing-url",
        "invalid-operation",
        "search-forbids-url",
        "scrape-forbids-query",
        "query-type",
        "mobile-type",
        "invalid-format",
        "negative-wait",
        "wait-type",
    ],
)
async def test_invalid_arguments_fail_without_external_requests(executor, firecrawl_dependencies, operation, arguments, expected_detail):
    payload = json.loads(await executor.execute(operation=operation, **arguments))

    assert payload["status"] == "failed"
    assert payload["operation"] == operation
    assert expected_detail in payload["error"]
    firecrawl_dependencies.app_factory.assert_not_called()
    firecrawl_dependencies.sdk.search.assert_not_awaited()
    firecrawl_dependencies.sdk.scrape_url.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_locale", ["zh", "en"], indirect=True)
async def test_missing_api_key_returns_localized_error_without_external_request(executor, firecrawl_dependencies, current_locale):
    executor.set_config(ProfileConfig.model_validate({"tool": {"firecrawl_api_key": ""}}))

    payload = json.loads(await executor.execute(operation="search", query="topic"))

    expected_error = {
        "zh": "Firecrawl API Key 未配置，请前往系统配置中的工具设置完成配置",
        "en": "Firecrawl API key is not configured. Please configure it in tool settings",
    }[current_locale]
    assert payload == {"error": expected_error, "system_info": SYSTEM_INFO}
    firecrawl_dependencies.app_factory.assert_not_called()
    firecrawl_dependencies.sdk.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_locale", ["zh", "en"], indirect=True)
@pytest.mark.parametrize(
    ("exception_message", "error_key"),
    [
        ("401 Unauthorized", "auth"),
        ("Unauthorized", "auth"),
        ("'NoneType' object has no attribute 'request'", "network"),
    ],
)
async def test_sdk_errors_are_translated(executor, firecrawl_dependencies, current_locale, exception_message, error_key):
    expected_errors = {
        "auth": {
            "zh": "Firecrawl API Key 认证失败或已失效，请检查并更新配置",
            "en": "Firecrawl API key authentication failed or expired. Please update the configuration",
        },
        "network": {
            "zh": "Firecrawl 服务调用失败，请检查网络连接或服务状态",
            "en": "Firecrawl request failed. Please check network connectivity or service status",
        },
    }
    firecrawl_dependencies.sdk.search.side_effect = RuntimeError(exception_message)

    payload = json.loads(await executor.execute(operation="search", query="topic"))

    assert payload == {"error": expected_errors[error_key][current_locale], "system_info": SYSTEM_INFO}
    firecrawl_dependencies.sdk.search.assert_awaited_once_with(query="topic", limit=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments", "method_name"),
    [
        ("search", {"query": "topic"}, "search"),
        ("scrape", {"url": URL}, "scrape_url"),
    ],
)
async def test_cancellation_propagates_from_sdk(executor, firecrawl_dependencies, operation, arguments, method_name):
    sdk_method = getattr(firecrawl_dependencies.sdk, method_name)
    sdk_method.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(operation=operation, **arguments)

    sdk_method.assert_awaited_once()
    firecrawl_dependencies.app_factory.assert_called_once_with(api_key=API_KEY)
