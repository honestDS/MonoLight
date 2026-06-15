import os
import sys

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.paths import SQLITE_DB_PATH, TEST_SESSION_DB_PATH, ensure_data_dirs

load_dotenv()

ensure_data_dirs()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = f"sqlite+aiosqlite:///{SQLITE_DB_PATH}"

if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
    DATABASE_URL = f"sqlite+aiosqlite:///{TEST_SESSION_DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
