import json
from unittest.mock import MagicMock, patch
import pytest
from app.core.tools.firecrawl_search import FirecrawlSearchExecutor
from app.core.tools.firecrawl_scrape import FirecrawlScrapeExecutor

@pytest.mark.asyncio
async def test_firecrawl_search_success():
    mock_results = {"success": True, "data": [{"url": "https://www.17173.com", "title": "17173"}]}
    
    with patch("app.core.tools.firecrawl_search.FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        # 模拟返回具有 model_dump 方法的对象（类似 Pydantic）
        mock_response = MagicMock()
        mock_response.model_dump.return_value = mock_results
        mock_app_instance.search.return_value = mock_response
        
        # 在 patch 之后创建 executor
        executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
        
        result_json = await executor.execute(query="17173", limit=3, location="China")
        result = json.loads(result_json)
        
        assert result["status"] == "success"
        assert result["results"] == mock_results
        mock_app_instance.search.assert_called_once_with(
            query="17173", 
            limit=3, 
            location="China", 
            scrape_options={'formats': ['markdown']}
        )

@pytest.mark.asyncio
async def test_firecrawl_search_failure():
    with patch("app.core.tools.firecrawl_search.FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        mock_app_instance.search.side_effect = Exception("API Error")
        
        executor = FirecrawlSearchExecutor(project_root="/tmp/monobot_test", uid="test_user")
        
        result_json = await executor.execute(query="test query")
        result = json.loads(result_json)
        
        assert "error" in result
        assert result["error"] == "API Error"

@pytest.mark.asyncio
async def test_firecrawl_scrape_success():
    mock_doc = {"success": True, "markdown": "# 17173 Test Content"}
    
    with patch("app.core.tools.firecrawl_scrape.FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        # 模拟返回具有 model_dump 方法的对象
        mock_response = MagicMock()
        mock_response.model_dump.return_value = mock_doc
        mock_app_instance.scrape_url.return_value = mock_response
        
        executor = FirecrawlScrapeExecutor(project_root="/tmp/monobot_test", uid="test_user")
        
        result_json = await executor.execute(url="https://www.17173.com", formats=["markdown"], mobile=True)
        result = json.loads(result_json)
        
        assert result["status"] == "success"
        assert result["data"] == mock_doc
        mock_app_instance.scrape_url.assert_called_once_with("https://www.17173.com", formats=["markdown"], mobile=True)

@pytest.mark.asyncio
async def test_firecrawl_scrape_failure():
    with patch("app.core.tools.firecrawl_scrape.FirecrawlApp") as MockApp:
        mock_app_instance = MockApp.return_value
        mock_app_instance.scrape_url.side_effect = Exception("Scrape Error")
        
        executor = FirecrawlScrapeExecutor(project_root="/tmp/monobot_test", uid="test_user")
        
        result_json = await executor.execute(url="https://www.17173.com")
        result = json.loads(result_json)
        
        assert "error" in result
        assert result["error"] == "Scrape Error"
