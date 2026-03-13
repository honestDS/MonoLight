import os
import uvicorn
from app.api.v1.providers import router as provider_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.profile import router as profile_router
from fastapi import FastAPI
from app.providers.database import engine, Base
from app.models import provider

app = FastAPI(title='Monobot API', version='1.0.0')
app.include_router(provider_router, prefix='/api/v1')
app.include_router(auth_router, prefix='/api/v1/auth', tags=['auth'])
app.include_router(chat_router, prefix='/api/v1')
app.include_router(profile_router, prefix='/api/v1')

@app.on_event('startup')
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get('/')
async def root():
    return {'status': 'MonoLight is running'}

if __name__ == '__main__':
    port = int(os.getenv('APP_PORT', 8000))
    uvicorn.run('main:app', host='0.0.0.0', port=port, reload=True)