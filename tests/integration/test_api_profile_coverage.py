import pytest
import importlib
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, delete, func
from app.models.user import User
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.models.prompt import PromptLibrary
from app.providers.database import get_db

@pytest.mark.asyncio
async def test_profile_full_coverage(db_session):
    import app.api.v1.profile as profile_module
    importlib.reload(profile_module)
    from main import app
    
    await db_session.execute(delete(Profile))
    await db_session.execute(delete(ModelProvider))
    await db_session.execute(delete(PromptLibrary))
    await db_session.execute(delete(User))
    
    admin_user = User(uid="admin_uid", username="admin_p", is_superuser=True, is_active=True)
    normal_user = User(uid="normal_uid", username="normal_p", is_superuser=False, is_active=True)
    db_session.add_all([admin_user, normal_user])
    
    provider = ModelProvider(id=1, name="test_provider", api_key="test", base_url="test")
    prompt = PromptLibrary(id=1, name="test_prompt", content="test")
    db_session.add_all([provider, prompt])
    await db_session.commit()

    current_test_user = admin_user
    async def override_get_current_user():
        return current_test_user
    
    async def override_get_db():
        yield db_session

    from app.core.security import get_current_user
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. check_admin_privilege (Forbidden)
        current_test_user = normal_user
        res = await client.post("/api/v1/profiles/create", json={"name": "p0", "provider_id": 1, "model_id": "m1"})
        assert res.status_code == 403
        
        current_test_user = admin_user
        
        # 2. create_profile
        # 2.1 Provider 不存在
        res = await client.post("/api/v1/profiles/create", json={"name": "p1", "provider_id": 999, "model_id": "m1"})
        assert res.status_code == 400
        
        # 2.2 成功创建 (包含 prompt_id)
        res = await client.post("/api/v1/profiles/create", json={"name": "p1", "provider_id": 1, "model_id": "m1", "prompt_id": 1})
        assert res.status_code == 200
        p1_id = res.json()["data"]["id"]
        
        # 2.3 名称已存在
        res = await client.post("/api/v1/profiles/create", json={"name": "p1", "provider_id": 1, "model_id": "m1"})
        assert res.status_code == 400
        
        # 2.4 Prompt 不存在
        res = await client.post("/api/v1/profiles/create", json={"name": "p2", "provider_id": 1, "model_id": "m1", "prompt_id": 999})
        assert res.status_code == 400

        # 3. list_profiles
        res = await client.get("/api/v1/profiles/list")
        assert res.status_code == 200

        # 4. activate_profile
        # 4.1 不存在
        res = await client.post("/api/v1/profiles/activate", params={"profile_id": 999})
        assert res.status_code == 404
        
        # 4.2 无 Provider
        p_obj = await db_session.get(Profile, p1_id)
        p_obj.provider_id = None
        await db_session.commit()
        res = await client.post("/api/v1/profiles/activate", params={"profile_id": p1_id})
        assert res.status_code == 400
        
        # 4.3 成功激活
        p_obj.provider_id = 1
        await db_session.commit()
        res = await client.post("/api/v1/profiles/activate", params={"profile_id": p1_id})
        assert res.status_code == 200

        # 5. update_profile
        # 5.1 不存在
        res = await client.post("/api/v1/profiles/update", params={"profile_id": 999}, json={"name": "new"})
        assert res.status_code == 404
        
        # 5.2 名称冲突
        await client.post("/api/v1/profiles/create", json={"name": "p2", "provider_id": 1, "model_id": "m1"})
        res = await client.post("/api/v1/profiles/update", params={"profile_id": p1_id}, json={"name": "p2"})
        assert res.status_code == 400
        
        # 5.3 Prompt 不存在
        res = await client.post("/api/v1/profiles/update", params={"profile_id": p1_id}, json={"prompt_id": 999})
        assert res.status_code == 404
        
        # 5.4 成功更新
        res = await client.post("/api/v1/profiles/update", params={"profile_id": p1_id}, json={"name": "p1_new", "prompt_id": 1})
        assert res.status_code == 200

        # 6. delete_profile
        # 6.1 不存在
        res = await client.post("/api/v1/profiles/delete", params={"profile_id": 999})
        assert res.status_code == 404
        
        # 6.2 删除最后一个
        res_list = await client.get("/api/v1/profiles/list")
        p2_id = [p["id"] for p in res_list.json()["data"] if p["name"] == "p2"][0]
        await client.post("/api/v1/profiles/delete", params={"profile_id": p2_id}) 
        res = await client.post("/api/v1/profiles/delete", params={"profile_id": p1_id})
        assert res.status_code == 400
        
        # 6.3 删除激活中的
        res_p3 = await client.post("/api/v1/profiles/create", json={"name": "p3", "provider_id": 1, "model_id": "m1"})
        p3_id = res_p3.json()["data"]["id"]
        # p1 是激活的
        res = await client.post("/api/v1/profiles/delete", params={"profile_id": p1_id})
        assert res.status_code == 400
        
        # 6.4 成功删除
        res = await client.post("/api/v1/profiles/delete", params={"profile_id": p3_id})
        assert res.status_code == 200

    app.dependency_overrides.clear()
