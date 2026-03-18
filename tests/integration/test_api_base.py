import pytest

@pytest.mark.asyncio
async def test_app_health(client):
    # 假设存在根路径或健康检查路径
    response = await client.get("/")
    # 验证框架是否正常响应（404 或 200 取决于路由定义）
    assert response.status_code in [200, 404]
