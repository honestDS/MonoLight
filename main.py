from app.core import messages
import os
import uvicorn
from app.api.v1.providers import router as provider_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router as prompt_router
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.schemas.response import UnifiedResponse
from sqlalchemy.exc import SQLAlchemyError

from fastapi.middleware.cors import CORSMiddleware
from app.providers.database import engine, Base, AsyncSessionLocal
from app.models import provider

app = FastAPI(title='Monobot API', version='1.0.0')


from sqlalchemy.exc import SQLAlchemyError

@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(
        status_code=500,
        content=UnifiedResponse.error(code=500, message=messages.ERR_DB_OPERATION_FAILED).model_dump()
    )

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=UnifiedResponse.error(code=exc.status_code, message=(exc.detail if isinstance(exc.detail, str) else str(exc.detail))).model_dump()
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=UnifiedResponse.error(code=422, message=messages.ERR_VALIDATION_FAILED + ": " + str(exc.errors())).model_dump()
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=UnifiedResponse.error(code=500, message=messages.ERR_INTERNAL_SERVER_ERROR + ": " + str(exc)).model_dump()
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
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