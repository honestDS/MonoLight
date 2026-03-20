import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

load_dotenv()

# 获取项目根目录
root_dir = Path(__file__).resolve().parent.parent.parent
default_db_path = root_dir / "monolight.db"

# 获取基础连接地址
DATABASE_URL = os.getenv("DATABASE_URL")

# 如果未配置，默认使用 monolight.db
if not DATABASE_URL:
    DATABASE_URL = f"sqlite+aiosqlite:///{default_db_path}"

# 【终极安全锁】如果处于测试环境，强制重定向到系统临时目录
if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
    import tempfile
    temp_db = os.path.join(tempfile.gettempdir(), "monolight_test_session.db")
    DATABASE_URL = f"sqlite+aiosqlite:///{temp_db}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
