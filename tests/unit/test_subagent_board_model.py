from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models  # noqa: F401
from app.core.crud.session.subagent import subagent_crud
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentWork
from app.models.user import User
from scripts.migration_20261109_02_add_subagent_board_post import migrate as migrate_board_post


@pytest_asyncio.fixture
async def sqlite_db() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        await conn.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(text("PRAGMA foreign_keys = ON"))
        yield session
    await engine.dispose()


def test_subagent_board_post_model_instantiation() -> None:
    post = SubagentBoardPost(
        uid="u1",
        session_id="s1",
        profile_id=1,
        subagent_id="sub1",
        child_work_id=10,
        stage="init",
        content="initialize task",
    )
    assert post.id is None
    assert post.uid == "u1"
    assert post.session_id == "s1"
    assert post.profile_id == 1
    assert post.subagent_id == "sub1"
    assert post.child_work_id == 10
    assert post.stage == "init"
    assert post.content == "initialize task"
    assert post.created_at is not None
    assert SubagentBoardPost.__tablename__ == "subagent_board_post"


@pytest.mark.asyncio
async def test_subagent_board_post_crud_lifecycle(sqlite_db: AsyncSession) -> None:
    db = sqlite_db
    user = User(uid="u1", username="u1")
    db.add(user)
    profile = Profile(id=1, uid="u1", name="p1", configs={})
    db.add(profile)
    session = ChatSession(session_id="s1", uid="u1", profile_id=1)
    db.add(session)
    work1 = SessionReplyWorkItem(
        id=10,
        uid="u1",
        session_id="s1",
        profile_id=1,
        sequence_no=1,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        status=SessionReplyWorkStatus.RUNNING,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        dedupe_key="k1",
    )
    work2 = SessionReplyWorkItem(
        id=20,
        uid="u1",
        session_id="s1",
        profile_id=1,
        sequence_no=2,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        status=SessionReplyWorkStatus.RUNNING,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        dedupe_key="k2",
    )
    work3 = SessionReplyWorkItem(
        id=30,
        uid="u1",
        session_id="s1",
        profile_id=1,
        sequence_no=3,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        status=SessionReplyWorkStatus.RUNNING,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        dedupe_key="k3",
    )
    db.add(work1)
    db.add(work2)
    db.add(work3)
    subagent1 = Subagent(id="sub1", uid="u1", session_id="s1", profile_id=1, name="sub1")
    subagent2 = Subagent(id="sub2", uid="u1", session_id="s1", profile_id=1, name="sub2")
    db.add(subagent1)
    db.add(subagent2)
    sw1 = SubagentWork(child_work_id=10, subagent_id="sub1", current_work_id=10)
    sw2 = SubagentWork(child_work_id=20, subagent_id="sub1", current_work_id=20)
    sw3 = SubagentWork(child_work_id=30, subagent_id="sub2", current_work_id=30)
    db.add(sw1)
    db.add(sw2)
    db.add(sw3)
    await db.commit()

    # 初始状态无看板条目
    empty_posts = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1"
    )
    assert empty_posts == []
    assert await subagent_crud.get_latest_board_post(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1"
    ) is None

    # 验证当 child_work_id 不属于指定 subagent_id 时，写入被拒绝
    with pytest.raises(ValueError, match="SubagentWork round not found"):
        await subagent_crud.create_board_post(
            db,
            uid="u1",
            session_id="s1",
            profile_id=1,
            subagent_id="sub1",
            child_work_id=30,  # 30 属于 sub2
            stage="invalid",
            content="should fail",
        )

    with pytest.raises(ValueError, match="Scoped subagent not found"):
        await subagent_crud.create_board_post(
            db,
            uid="u2",
            session_id="s1",
            profile_id=1,
            subagent_id="sub1",
            child_work_id=10,
            stage="invalid-scope-uid",
            content="should fail",
        )

    with pytest.raises(ValueError, match="Scoped subagent not found"):
        await subagent_crud.create_board_post(
            db,
            uid="u1",
            session_id="s2",
            profile_id=1,
            subagent_id="sub1",
            child_work_id=10,
            stage="invalid-scope-session",
            content="should fail",
        )

    with pytest.raises(ValueError, match="Scoped subagent not found"):
        await subagent_crud.create_board_post(
            db,
            uid="u1",
            session_id="s1",
            profile_id=2,
            subagent_id="sub1",
            child_work_id=10,
            stage="invalid",
            content="should fail",
        )

    # 模拟 SubagentWork 存在但对应工作项作用域错配场景
    user2 = User(uid="u2", username="u2")
    db.add(user2)
    session2 = ChatSession(session_id="s2", uid="u2", profile_id=1)
    db.add(session2)
    await db.flush()

    wrong_scope_work = SessionReplyWorkItem(
        id=99,
        uid="u2",
        session_id="s2",
        profile_id=1,
        sequence_no=99,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=1,
        status=SessionReplyWorkStatus.RUNNING,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        dedupe_key="k99",
    )
    db.add(wrong_scope_work)
    wrong_subagent_work = SubagentWork(child_work_id=99, subagent_id="sub1", current_work_id=99)
    db.add(wrong_subagent_work)
    await db.commit()
    with pytest.raises(ValueError, match="Work item scope mismatch"):
        await subagent_crud.create_board_post(
            db,
            uid="u1",
            session_id="s1",
            profile_id=1,
            subagent_id="sub1",
            child_work_id=99,
            stage="invalid",
            content="should fail",
        )

    # 第一轮新增两条进度
    post1 = await subagent_crud.create_board_post(
        db,
        uid="u1",
        session_id="s1",
        profile_id=1,
        subagent_id="sub1",
        child_work_id=10,
        stage="plan",
        content="planning architecture",
    )
    post2 = await subagent_crud.create_board_post(
        db,
        uid="u1",
        session_id="s1",
        profile_id=1,
        subagent_id="sub1",
        child_work_id=10,
        stage="impl",
        content="writing code",
    )
    # 第二轮新增一条进度
    post3 = await subagent_crud.create_board_post(
        db,
        uid="u1",
        session_id="s1",
        profile_id=1,
        subagent_id="sub1",
        child_work_id=20,
        stage="verify",
        content="running checks",
    )
    # 另一子代理新增一条进度
    await subagent_crud.create_board_post(
        db,
        uid="u1",
        session_id="s1",
        profile_id=1,
        subagent_id="sub2",
        child_work_id=30,
        stage="research",
        content="subagent2 notes",
    )
    await db.commit()

    assert post1.id is not None
    assert post2.id is not None
    assert post3.id is not None
    assert post1.id < post2.id < post3.id

    # 查询 sub1 的全部看板进度（按 id 正序）
    sub1_all = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1"
    )
    assert len(sub1_all) == 3
    assert [p.stage for p in sub1_all] == ["plan", "impl", "verify"]
    assert [p.content for p in sub1_all] == ["planning architecture", "writing code", "running checks"]

    # 按 child_work_id 过滤
    sub1_round1 = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1", child_work_id=10
    )
    assert len(sub1_round1) == 2
    assert [p.stage for p in sub1_round1] == ["plan", "impl"]

    sub1_round2 = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1", child_work_id=20
    )
    assert len(sub1_round2) == 1
    assert sub1_round2[0].stage == "verify"

    # 查询最新进度
    latest_sub1 = await subagent_crud.get_latest_board_post(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1"
    )
    assert latest_sub1 is not None
    assert latest_sub1.id == post3.id
    assert latest_sub1.stage == "verify"

    latest_sub1_round1 = await subagent_crud.get_latest_board_post(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1", child_work_id=10
    )
    assert latest_sub1_round1 is not None
    assert latest_sub1_round1.id == post2.id
    assert latest_sub1_round1.stage == "impl"

    # 验证独立于插入顺序的显式 ORDER BY id ASC 排序
    post_high = SubagentBoardPost(
        id=200, uid="u1", session_id="s1", profile_id=1,
        subagent_id="sub1", child_work_id=10, stage="later", content="higher id"
    )
    post_low = SubagentBoardPost(
        id=150, uid="u1", session_id="s1", profile_id=1,
        subagent_id="sub1", child_work_id=10, stage="earlier", content="lower id"
    )
    db.add(post_high)
    await db.flush()
    db.add(post_low)
    await db.flush()
    await db.commit()
    posts_with_custom_id = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1", child_work_id=10
    )
    custom_ids = [p.id for p in posts_with_custom_id if p.id in (150, 200)]
    assert custom_ids == [150, 200]

    # 权限与范围隔离：不同 uid、session_id 或 profile_id 无法查到
    isolated_posts_by_uid = await subagent_crud.list_board_posts(
        db, uid="u2", session_id="s1", profile_id=1, subagent_id="sub1"
    )
    assert isolated_posts_by_uid == []

    isolated_posts_by_session = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s2", profile_id=1, subagent_id="sub1"
    )
    assert isolated_posts_by_session == []

    isolated_posts_by_profile = await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=2, subagent_id="sub1"
    )
    assert isolated_posts_by_profile == []

    isolated_latest_by_uid = await subagent_crud.get_latest_board_post(
        db, uid="u2", session_id="s1", profile_id=1, subagent_id="sub1"
    )
    assert isolated_latest_by_uid is None

    isolated_latest_by_session = await subagent_crud.get_latest_board_post(
        db, uid="u1", session_id="s2", profile_id=1, subagent_id="sub1"
    )
    assert isolated_latest_by_session is None

    isolated_latest_by_profile = await subagent_crud.get_latest_board_post(
        db, uid="u1", session_id="s1", profile_id=2, subagent_id="sub1"
    )
    assert isolated_latest_by_profile is None

    await db.delete(session)
    await db.commit()

    assert await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub1"
    ) == []
    assert await subagent_crud.list_board_posts(
        db, uid="u1", session_id="s1", profile_id=1, subagent_id="sub2"
    ) == []


@pytest.mark.asyncio
async def test_subagent_board_post_migration_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # 首次执行迁移
        await migrate_board_post(session)
        connection = await session.connection()
        table_names = await connection.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert "subagent_board_post" in table_names
        columns = await connection.run_sync(
            lambda sync_conn: [col["name"] for col in inspect(sync_conn).get_columns("subagent_board_post")]
        )
        assert {"id", "uid", "session_id", "profile_id", "subagent_id", "child_work_id", "stage", "content", "created_at"}.issubset(set(columns))
        # 重复执行迁移，验证幂等
        await migrate_board_post(session)
    await engine.dispose()
