import pytest
from sqlalchemy import inspect
from app.providers.database import engine, Base

@pytest.mark.asyncio
async def test_tables_alignment(db_session):
    """验证所有物理表名是否已按单数对齐"""

    def get_tables(connection):
        inst = inspect(connection)
        return inst.get_table_names()

    # 确保测试库中的表已被创建
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        tables = await conn.run_sync(get_tables)

    # 验证单数表名是否存在
    expected_tables = ["user", "provider", "profile", "prompt", "message"]
    for table in expected_tables:
        assert table in tables
