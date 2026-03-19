import sys
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from pathlib import Path
import os
import shutil
from dotenv import load_dotenv

load_dotenv()


def prepare_database():
    """
    数据库预处理逻辑：
    1. 优先检查 monobot.db 是否存在，存在则更名为 monolight.db
    2. 若 monolight.db 也不存在，则使用 .env 中的配置或新建
    """
    # 使用 pathlib 获取项目根目录 (app/providers/database.py -> app/providers -> app -> root)
    root_dir = Path(__file__).resolve().parent.parent.parent
    old_db = root_dir / "monobot.db"
    target_db = root_dir / "monolight.db"

    if old_db.exists():
        if target_db.exists():
            # 如果目标已存在，先备份原有的目标文件
            shutil.move(str(target_db), str(target_db) + ".migration.bak")
        shutil.move(str(old_db), str(target_db))

    # 构造 SQLite 异步连接字符串
    return f"sqlite+aiosqlite:///{target_db}"


# 获取基础连接地址
DATABASE_URL = os.getenv("DATABASE_URL")

# 如果未配置或使用 SQLite，执行基础路径预处理（包括 monolight.db 的迁移逻辑）
if not DATABASE_URL or "sqlite" in DATABASE_URL:
    DATABASE_URL = prepare_database()

# 【终极安全锁】如果处于测试环境，无论之前计算出什么路径，强制重定向到系统临时目录
if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
    # 仅当 DATABASE_URL 为空或指向生产库时触发强制重定向
    # 允许通过环境变量显式注入自定义测试库路径
    env_db = os.getenv("DATABASE_URL")
    if not env_db or "monolight.db" in DATABASE_URL:
        import tempfile

        temp_db = os.path.join(tempfile.gettempdir(), "monobot_test_session.db")
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
