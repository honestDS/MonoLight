import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import app.warning_filters  # noqa: F401
from app.api.v1.auth import router as auth_router
from app.api.v1.channels import router as channel_router
from app.api.v1.chat import router as chat_router
from app.api.v1.files import router as files_router
from app.api.v1.knowledge_base import router as knowledge_base_router
from app.api.v1.message_platforms import router as message_platform_router
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router as prompt_router
from app.api.v1.scheduled_tasks import router as scheduled_task_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as user_router
from app.core.log import LogManager, get_logger
from app.core.log_broadcaster import log_broadcaster
from app.core.paths import DATA_DIR, DEFAULT_LOG_FILE_PATH, TEMP_DIR
from app.core.session_notifier import session_notifier
from app.core.utils.time import get_local_time
from app.handler import register_handlers, register_middlewares
from app.providers.database import AsyncSessionLocal
from app.providers.database.bootstrap import init_database_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 兼容直接运行 main.py 或 uvicorn main:app，不能依赖父启动器预先建表或迁移。
    async with AsyncSessionLocal() as session:
        await init_database_schema(session)
    await session_notifier.start()

    # 记录启动时的信息，确保此时异步环境已就绪，日志能够入库
    now_aware = get_local_time()
    log_file_path = os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH))
    LogManager.setup(
        log_path=log_file_path,
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    await log_broadcaster.start()

    get_logger("app.core.log").info(f"Log system initialized. Path: {log_file_path} | Time: {now_aware.isoformat()}")

    yield

    await log_broadcaster.stop()
    await session_notifier.stop()


def register_routers(app: FastAPI) -> None:
    # 使用 APIRouter 内部定义的 tags，避免在 include_router 时重复或冲突定义
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
    app.include_router(user_router, prefix="/api/v1/admin")
    app.include_router(channel_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1", tags=["Files"])
    app.include_router(profile_router, prefix="/api/v1")
    app.include_router(prompt_router, prefix="/api/v1")
    app.include_router(knowledge_base_router, prefix="/api/v1")
    app.include_router(message_platform_router, prefix="/api/v1")
    app.include_router(scheduled_task_router, prefix="/api/v1")


def create_app() -> FastAPI:
    fastapi_app = FastAPI(lifespan=lifespan, title="Monolight API", version="1.0.0")
    register_middlewares(fastapi_app)
    register_handlers(fastapi_app)
    register_routers(fastapi_app)
    return fastapi_app


app = create_app()


if __name__ == "__main__":
    port_env = os.getenv("APP_PORT")
    if not port_env:
        raise ValueError("APP_PORT must be set in .env file")
    port = int(port_env)
    # 限制监控目录为 app 目录，从而排除 temp 和其他非代码目录
    # 同时保留 reload_excludes 作为双重保险，并规范路径格式
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        reload_dirs=["app"],
        reload_excludes=[TEMP_DIR.name, DATA_DIR.name, "*.log"],
    )
