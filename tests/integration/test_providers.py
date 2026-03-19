from app.core.exceptions import ResourceNotFoundException
import pytest
import uuid
from app.core.security import get_password_hash
from app.models.user import User


@pytest.fixture(scope="function")
async def test_user_token(client, db_session):
    """创建用户并登录获取 Token"""
    test_username = f"user_{uuid.uuid4().hex[:8]}"
    test_password = "test_password"

    hashed_pw = get_password_hash(test_password)
    user = User(
        uid=uuid.uuid4().hex,
        username=test_username,
        hashed_password=hashed_pw,
        is_active=True,
        is_superuser=True,
    )
    db_session.add(user)
    await db_session.commit()

    login_payload = {"username": test_username, "password": test_password}
    response = await client.post("/api/v1/auth/login", json=login_payload)
    assert response.status_code == 200
    token_data = response.json()["data"]
    return token_data["access_token"]


@pytest.mark.asyncio
async def test_providers_workflow(client, db_session, test_user_token):
    """测试 Provider 业务流 (路径精准对齐)"""
    headers = {"Authorization": f"Bearer {test_user_token}"}

    # 1. 测试列表查询 (GET /api/v1/providers/list)
    res_list = await client.get("/api/v1/providers/list", headers=headers)
    assert res_list.status_code == 200
    assert "data" in res_list.json()

    # 2. 测试创建 Provider (POST /api/v1/providers/create)
    create_payload = {
        "name": f"Provider_{uuid.uuid4().hex[:6]}",
        "provider_type": "OPENAI",
        "api_key": "sk-test-key",
        "base_url": "https://api.openai.com/v1",
        "is_active": True,
    }
    res_create = await client.post(
        "/api/v1/providers/create", json=create_payload, headers=headers
    )
    assert res_create.status_code == 200
    provider_id = res_create.json()["data"]["id"]

    # 3. 测试获取详情 (GET /api/v1/providers/get?provider_id=X)
    res_get = await client.get(
        f"/api/v1/providers/get?provider_id={provider_id}", headers=headers
    )
    assert res_get.status_code == 200
    assert res_get.json()["data"]["id"] == provider_id

    # 3.5 测试更新 Provider (POST /api/v1/providers/update?provider_id=X)
    update_payload = {
        "name": f"Updated_{uuid.uuid4().hex[:6]}",
        "api_key": "sk-updated-key",
    }
    res_update = await client.post(
        f"/api/v1/providers/update?provider_id={provider_id}",
        json=update_payload,
        headers=headers,
    )
    assert res_update.status_code == 200
    assert res_update.json()["data"]["name"].startswith("Updated_")

    # 4. 测试删除 (POST /api/v1/providers/delete?provider_id=X)
    res_del = await client.post(
        f"/api/v1/providers/delete?provider_id={provider_id}", headers=headers
    )
    assert res_del.status_code == 200

    # 5. 验证已删除：统一处理可能抛出的业务异常
    try:
        res_final = await client.get(
            f"/api/v1/providers/get?provider_id={provider_id}", headers=headers
        )
        # 如果没有抛出异常（即有异常处理器生效），则验证状态码
        assert res_final.status_code in [404, 422]
    except ResourceNotFoundException:
        # 如果测试环境下异常直接抛出，捕获它即视为验证通过（模型确实不存在）
        pass
    except Exception as e:
        # 捕获其他可能的业务异常并根据名称判断
        assert "NotFound" in e.__class__.__name__ or "Parameter" in e.__class__.__name__


@pytest.mark.asyncio
async def test_providers_unauthorized(client):
    """测试未认证拦截 (针对 /api/v1/providers/list)"""
    response = await client.get("/api/v1/providers/list")
    assert response.status_code == 401
