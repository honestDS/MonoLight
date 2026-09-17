import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.api.v1.profile import PROFILE_TOOL_OPTIONS
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher import inject_system_prompt as inject_system_prompt_module
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    ManagedKnowledgeItem,
)
from app.models.profile import PROFILE_EXAMPLE, Profile, ToolConfig


def _profile(*, memory_enabled: bool, enabled_tools: list[str] | None = None) -> Profile:
    return Profile(
        id=9,
        uid="user-1",
        name="knowledge_query_exposure-profile",
        configs={
            "tool": {"enabled_tools": enabled_tools if enabled_tools is not None else []},
            "memory": {"enabled": memory_enabled},
        },
    )


def _tool_names(tools: list[dict]) -> set[str]:
    return {tool["function"]["name"] for tool in tools}


async def _add_knowledge_base(
    session: AsyncSession,
    *,
    name: str,
    knowledge_base_type: KnowledgeBaseType,
    index_status: KnowledgeBaseIndexStatus,
    with_document: bool,
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        uid="user-1",
        name=name,
        embedding_channel_id=1,
        embedding_model_id="embedding-model",
        embedding_dimensions=3,
        collection_name=f"{name}-legacy",
        knowledge_base_type=knowledge_base_type,
        managed_profile_id=9 if knowledge_base_type == KnowledgeBaseType.LLM_MANAGED else None,
        active_embedding_channel_id=1,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=3,
        active_embedding_signature=f"sig-{name}",
        active_embedding_revision=1,
        active_collection_name=f"{name}-active",
        index_status=index_status,
    )
    session.add(knowledge_base)
    await session.flush()
    session.add(
        KnowledgeBaseProfileBinding(
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            profile_id=9,
        )
    )
    if with_document and knowledge_base_type == KnowledgeBaseType.USER:
        session.add(
            KnowledgeBaseDocument(
                knowledge_base_id=knowledge_base.id,
                filename=f"{name}.md",
                content="published content",
                chunk_size=1000,
                chunk_overlap=100,
                batch_size=16,
                chunk_count=1,
                chunk_ids=[f"{name}-chunk-1"],
            )
        )
    await session.commit()
    return knowledge_base


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = (
        KnowledgeBase.__table__,
        KnowledgeBaseCollectionOwner.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        ManagedKnowledgeItem.__table__,
    )
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_query_knowledge_base_is_not_a_configurable_enabled_tool() -> None:
    assert "query_knowledge_base" not in ToolConfig().enabled_tools
    assert "query_knowledge_base" not in PROFILE_EXAMPLE["configs"]["tool"]["enabled_tools"]
    assert "query_knowledge_base" not in {item["value"] for item in PROFILE_TOOL_OPTIONS}


@pytest.mark.asyncio
async def test_memory_enabled_always_hides_independent_query_tool(db_session: AsyncSession) -> None:
    await _add_knowledge_base(
        db_session,
        name="user-ready",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=True,
    )

    tools, whitelist = await get_tools_for_profile(
        db_session,
        _profile(memory_enabled=True, enabled_tools=["query_knowledge_base"]),
    )

    assert "query_knowledge_base" not in _tool_names(tools)
    assert whitelist == []


@pytest.mark.asyncio
async def test_memory_disabled_exposes_published_user_knowledge_without_enabled_tools_entry(db_session: AsyncSession) -> None:
    knowledge_base = await _add_knowledge_base(
        db_session,
        name="user-ready",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=True,
    )

    tools, whitelist = await get_tools_for_profile(
        db_session,
        _profile(memory_enabled=False, enabled_tools=[]),
    )

    assert "query_knowledge_base" in _tool_names(tools)
    assert whitelist == [knowledge_base.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_status", "with_document"),
    [
        (KnowledgeBaseIndexStatus.READY, False),
        (KnowledgeBaseIndexStatus.PENDING, True),
        (KnowledgeBaseIndexStatus.REINDEXING, True),
        (KnowledgeBaseIndexStatus.FAILED, True),
    ],
)
async def test_empty_or_unavailable_user_knowledge_does_not_expose_query_tool(
    db_session: AsyncSession,
    index_status: KnowledgeBaseIndexStatus,
    with_document: bool,
) -> None:
    await _add_knowledge_base(
        db_session,
        name=f"user-{index_status.value}-{with_document}",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=index_status,
        with_document=with_document,
    )

    tools, whitelist = await get_tools_for_profile(
        db_session,
        _profile(memory_enabled=False, enabled_tools=["query_knowledge_base"]),
    )

    assert "query_knowledge_base" not in _tool_names(tools)
    assert whitelist == []


@pytest.mark.asyncio
async def test_independent_query_whitelist_excludes_managed_knowledge(db_session: AsyncSession) -> None:
    user_knowledge_base = await _add_knowledge_base(
        db_session,
        name="user-ready",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=True,
    )
    await _add_knowledge_base(
        db_session,
        name="managed-ready",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=False,
    )

    tools, whitelist = await get_tools_for_profile(
        db_session,
        _profile(memory_enabled=False, enabled_tools=["query_knowledge_base"]),
    )

    assert "query_knowledge_base" in _tool_names(tools)
    assert whitelist == [user_knowledge_base.id]


@pytest.mark.asyncio
async def test_memory_disabled_with_only_managed_knowledge_hides_query_tool(db_session: AsyncSession) -> None:
    managed_knowledge_base = await _add_knowledge_base(
        db_session,
        name="managed-ready",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=False,
    )
    db_session.add(
        ManagedKnowledgeItem(
            uid="user-1",
            knowledge_base_id=managed_knowledge_base.id,
            knowledge_key="managed-key",
            content="managed content",
            content_token_count=2,
            content_hash="a" * 64,
            version=1,
            indexed_version=1,
            vector_item_ids=["managed-1"],
            is_recallable=True,
        )
    )
    await db_session.commit()

    tools, whitelist = await get_tools_for_profile(
        db_session,
        _profile(memory_enabled=False, enabled_tools=["query_knowledge_base"]),
    )

    assert "query_knowledge_base" not in _tool_names(tools)
    assert whitelist == []


@pytest.mark.asyncio
async def test_memory_enabled_catalog_does_not_prompt_unexposed_query_tool(db_session: AsyncSession) -> None:
    await _add_knowledge_base(
        db_session,
        name="user-ready",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=True,
    )

    prompt = await inject_system_prompt_module.build_system_prompt(
        db_session,
        _profile(memory_enabled=True),
    )

    assert "user-ready" in prompt
    assert "query_knowledge_base" not in prompt


@pytest.mark.asyncio
async def test_memory_disabled_empty_catalog_does_not_prompt_query_tool(db_session: AsyncSession) -> None:
    await _add_knowledge_base(
        db_session,
        name="user-empty",
        knowledge_base_type=KnowledgeBaseType.USER,
        index_status=KnowledgeBaseIndexStatus.READY,
        with_document=False,
    )

    prompt = await inject_system_prompt_module.build_system_prompt(
        db_session,
        _profile(memory_enabled=False),
    )

    assert "query_knowledge_base" not in prompt
    assert "user-empty" not in prompt
