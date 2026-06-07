import pytest
from app.transformers.openai import OpenAITransformer
from app.models.message import InternalMessage, MessageRole, TextPart
import json

@pytest.mark.asyncio
async def test_openai_get_embeddings(monkeypatch):
    # Mock aiohttp ClientSession to avoid real network requests
    class MockResponse:
        def __init__(self, status, text_data):
            self.status = status
            self.text_data = text_data
            
        async def text(self):
            return self.text_data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    class MockSession:
        def __init__(self, *args, **kwargs):
            self.request_kwargs = None
            
        def post(self, url, **kwargs):
            self.request_kwargs = kwargs
            # Return a fake response matching OpenAI embedding format
            fake_response = {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [0.1, 0.2, 0.3],
                    }
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 5, "total_tokens": 5}
            }
            return MockResponse(200, json.dumps(fake_response))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)

    transformer = OpenAITransformer()
    
    # 1. Test with list of strings
    input_texts = ["Hello", "World"]
    print(f"\n[Test 1] Testing with input list: {input_texts}")
    res = await transformer.get_embeddings(
        api_key="fake-key",
        base_url="https://api.openai.com/v1",
        model_id="text-embedding-3-small",
        input_texts=input_texts,
        dimensions=1024
    )
    print(f"[Test 1] Response received: {json.dumps(res, indent=2)}")
    
    assert res["object"] == "list"
    assert len(res["data"]) == 1
    assert res["data"][0]["embedding"] == [0.1, 0.2, 0.3]

    # 2. Test with single string
    single_text = "Hello world"
    print(f"\n[Test 2] Testing with single string: '{single_text}'")
    res2 = await transformer.get_embeddings(
        api_key="fake-key",
        base_url="https://api.openai.com/v1",
        model_id="text-embedding-3-small",
        input_texts=single_text
    )
    print(f"[Test 2] Response received: {json.dumps(res2, indent=2)}")
    assert res2["object"] == "list"
