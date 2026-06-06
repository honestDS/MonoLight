import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

root_dir = Path(__file__).resolve().parent.parent.parent
default_db_path = root_dir / "monolight.db"
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = f"sqlite+aiosqlite:///{default_db_path}"

if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
    import tempfile

    temp_db = Path(tempfile.gettempdir()) / "monolight_test_session.db"
    DATABASE_URL = f"sqlite+aiosqlite:///{temp_db}"

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
