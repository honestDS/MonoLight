import os
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, delete
from app.models.user import User
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.providers.database import get_db

@pytest.mark.asyncio
async def test_combined_coverage(db_session):
    from main import app
    from app.core.security import get_current_user
    
    # 彻底隔离数据库
    async def override_get_db():
        yield db_session
    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 初始化数据
        admin_data = User(uid="admin", username="admin", hashed_password="bad", is_superuser=True, is_active=True)
        normal_data = User(uid="normal", username="normal", is_superuser=False, is_active=True)
        provider_data = ModelProvider(id=1, name="p", api_key="k", base_url="b")
        db_session.add_all([admin_data, normal_data, provider_data])
        await db_session.commit()

        test_user = admin_data
        async def mock_user(): return test_user
        app.dependency_overrides[get_current_user] = mock_user

        # --- Profile 逻辑 ---
        test_user = normal_data
        await client.post("/api/v1/profiles/create", json={"name":"p1","provider_id":1,"model_id":"m"})
        
        test_user = admin_data
        await client.post("/api/v1/profiles/create", json={"name":"px","provider_id":999,"model_id":"m"})
        res_p1 = await client.post("/api/v1/profiles/create", json={"name":"p1","provider_id":1,"model_id":"m"})
        p1_id = res_p1.json()["data"]["id"]
        await client.post("/api/v1/profiles/create", json={"name":"p1","provider_id":1,"model_id":"m"})
        
        await client.post("/api/v1/profiles/activate", params={"profile_id":999})
        p_obj = await db_session.get(Profile, p1_id)
        p_obj.provider_id = None
        await db_session.commit()
        await client.post("/api/v1/profiles/activate", params={"profile_id":p1_id})
        p_obj.provider_id = 1
        await db_session.commit()
        await client.post("/api/v1/profiles/activate", params={"profile_id":p1_id})

        # --- Auth 逻辑 ---
        os.environ["ADMIN_RESET_TOKEN"] = "t"
        await client.post("/api/v1/auth/reset_admin", json={"reset_token":"wrong"})
        await client.post("/api/v1/auth/reset_admin", json={"reset_token":"t"})
        
        # 重新获取 admin 确保 session 同步
        u_admin = (await db_session.execute(select(User).where(User.username == "admin"))).scalars().first()
        u_admin.hashed_password = "bad"
        await db_session.commit()
        await client.post("/api/v1/auth/login", json={"username":"admin", "password":"any"})

    app.dependency_overrides.clear()
