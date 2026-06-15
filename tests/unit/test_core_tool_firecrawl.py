import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.tools.firecrawl_scrape import FirecrawlScrapeExecutor, normalize_formats
from app.core.tools.firecrawl_search import FirecrawlSearchExecutor, normalize_scrape_options


@pytest.mark.asyncio
async def test_firecrawl_search_success():
    mock_results = {"success": True, "data": [{"url": "https://www.17173.com", "title": "17173"}]}

    with patch("app.core.tools.firecrawl_search.AsyncV1FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        # 模拟返回具有 model_dump 方法的对象（类似 Pydantic）
        mock_response = MagicMock()
        mock_response.model_dump.return_value = mock_results
        mock_app_instance.search = AsyncMock(return_value=mock_response)

        # 在 patch 之后创建 executor
        executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
        # 模拟已配置 API Key
        executor.cfg = MagicMock()
        executor.cfg.tool.firecrawl_api_key = "test_key"

        result_json = await executor.execute(query="17173", limit=3, location="China")
        result = json.loads(result_json)

        assert result["status"] == "success"
        assert result["results"] == mock_results

        mock_app_instance.search.assert_awaited_once()
        _, call_kwargs = mock_app_instance.search.await_args
        assert call_kwargs["query"] == "17173"
        assert call_kwargs["limit"] == 3
        assert call_kwargs["location"] == "China"
        assert call_kwargs["scrape_options"].model_dump(by_alias=True, exclude_none=True)["formats"] == ["markdown"]


@pytest.mark.asyncio
async def test_firecrawl_search_failure():
    with patch("app.core.tools.firecrawl_search.AsyncV1FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        mock_app_instance.search = AsyncMock(side_effect=Exception("API Error"))

        executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
        executor.cfg = MagicMock()
        executor.cfg.tool.firecrawl_api_key = "test_key"

        result_json = await executor.execute(query="test query")
        result = json.loads(result_json)

        assert "error" in result
        assert result["error"] == "API Error"


@pytest.mark.asyncio
async def test_firecrawl_scrape_success():
    mock_doc = {"success": True, "markdown": "# 17173 Test Content"}

    with patch("app.core.tools.firecrawl_scrape.AsyncV1FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        # 模拟返回具有 model_dump 方法的对象
        mock_response = MagicMock()
        mock_response.model_dump.return_value = mock_doc
        mock_app_instance.scrape_url = AsyncMock(return_value=mock_response)

        executor = FirecrawlScrapeExecutor(project_root="/tmp/monobot_test", uid="test_user")
        executor.cfg = MagicMock()
        executor.cfg.tool.firecrawl_api_key = "test_key"

        result_json = await executor.execute(url="https://www.17173.com", formats=["markdown"], mobile=True)
        result = json.loads(result_json)

        assert result["status"] == "success"
        assert result["data"] == mock_doc
        mock_app_instance.scrape_url.assert_awaited_once_with("https://www.17173.com", formats=["markdown"], mobile=True)


@pytest.mark.asyncio
async def test_firecrawl_scrape_failure():
    with patch("app.core.tools.firecrawl_scrape.AsyncV1FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        mock_app_instance.scrape_url = AsyncMock(side_effect=Exception("Scrape Error"))

        executor = FirecrawlScrapeExecutor(project_root="/tmp/monobot_test", uid="test_user")
        executor.cfg = MagicMock()
        executor.cfg.tool.firecrawl_api_key = "test_key"

        result_json = await executor.execute(url="https://www.17173.com")
        result = json.loads(result_json)

        assert "error" in result
        assert result["error"] == "Scrape Error"


def test_firecrawl_search_normalize_scrape_options():
    scrape_options = normalize_scrape_options(
        {
            "formats": ["markdown", "raw_html"],
            "only_main_content": True,
            "wait_for": 1000,
        }
    )

    assert scrape_options.model_dump(by_alias=True, exclude_none=True) == {
        "formats": ["markdown", "rawHtml"],
        "onlyMainContent": True,
        "waitFor": 1000,
        "timeout": 30000,
    }


def test_firecrawl_scrape_normalize_formats():
    assert normalize_formats(["markdown", "raw_html", "links"]) == ["markdown", "rawHtml", "links"]


@pytest.mark.asyncio
async def test_firecrawl_no_api_key():
    # 测试缺失 API Key 的情况
    executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
    executor.cfg = MagicMock()
    executor.cfg.tool.firecrawl_api_key = None

    result_json = await executor.execute(query="test")
    result = json.loads(result_json)

    assert "error" in result
    assert "API Key 未配置" in result["error"]


@pytest.mark.asyncio
async def test_firecrawl_auth_failure():
    # 测试认证失败的情况
    with patch("app.core.tools.firecrawl_search.AsyncV1FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        mock_app_instance.search = AsyncMock(side_effect=Exception("401 Unauthorized: Invalid API Key"))

        executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
        executor.cfg = MagicMock()
        executor.cfg.tool.firecrawl_api_key = "wrong_key"

        result_json = await executor.execute(query="test")
        result = json.loads(result_json)

        assert "error" in result
        assert "认证失败或已失效" in result["error"]
