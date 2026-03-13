import os
import uvicorn
from app.api.v1.providers import router as provider_router
from fastapi import FastAPI
from app.providers.database import engine, Base
from app.models import provider

app = FastAPI(title='Monobot API', version='1.0.0')
app.include_router(provider_router, prefix='/api/v1')

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
