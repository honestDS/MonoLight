import os
import uvicorn
from app.api.v1.providers import router as provider_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router as prompt_router
from fastapi import FastAPI
from app.providers.database import engine, Base, AsyncSessionLocal
from app.models import provider

app = FastAPI(title='Monobot API', version='1.0.0')
app.include_router(provider_router, prefix='/api/v1')
app.include_router(auth_router, prefix='/api/v1/auth', tags=['auth'])
app.include_router(chat_router, prefix='/api/v1')
app.include_router(profile_router, prefix='/api/v1')
app.include_router(prompt_router, prefix='/api/v1')

@app.on_event('startup')
async def startup():

    # 初始化默认 Profile
    from sqlalchemy import select
    from app.models.profile import Profile
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        check = await session.execute(select(Profile).where(Profile.name == 'default'))
        if not check.scalars().first():
            default_profile = Profile(
                name='default',
                provider_id=-1,
                model_id='gemini-3-flash-preview',
                temperature=0.7,
                top_p=1.0,
                max_tokens=0,
                stream=False,
                extra_config={'additionalProp1': {}},
                context_window_k=1024,
                is_active=True
            )
            session.add(default_profile)
            await session.commit()


@app.get('/')
async def root():
    return {'status': 'MonoLight is running'}

if __name__ == '__main__':
    port_env = os.getenv('APP_PORT')
    if not port_env:
        raise ValueError('APP_PORT must be set in .env file')
    port = int(port_env)
    uvicorn.run('main:app', host='0.0.0.0', port=port, reload=True)