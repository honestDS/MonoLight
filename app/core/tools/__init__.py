import copy
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.embedding.knowledge_base import build_knowledge_base_whitelist, is_embedding_profile_available, list_available_knowledge_bases
from app.core.log import get_logger
from app.models.profile import Profile

from .file_writer import FILE_WRITER_TOOL_SCHEMA, FileWriterExecutor
from .firecrawl_scrape import FIRECRAWL_SCRAPE_TOOL_SCHEMA, FirecrawlScrapeExecutor
from .firecrawl_search import FIRECRAWL_SEARCH_TOOL_SCHEMA, FirecrawlSearchExecutor
from .knowledge_base_query import KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA, KnowledgeBaseQueryExecutor
from .shell import SHELL_TOOL_SCHEMA, ShellExecutor

logger = get_logger(__name__)

# Tool Registry
ALL_TOOLS_SCHEMAS = [
    SHELL_TOOL_SCHEMA,
    FILE_WRITER_TOOL_SCHEMA,
    FIRECRAWL_SEARCH_TOOL_SCHEMA,
    FIRECRAWL_SCRAPE_TOOL_SCHEMA,
]

# Tool Executor Mapping
TOOL_EXECUTOR_MAP = {
    SHELL_TOOL_SCHEMA["function"]["name"]: ShellExecutor,
    FILE_WRITER_TOOL_SCHEMA["function"]["name"]: FileWriterExecutor,
    FIRECRAWL_SEARCH_TOOL_SCHEMA["function"]["name"]: FirecrawlSearchExecutor,
    FIRECRAWL_SCRAPE_TOOL_SCHEMA["function"]["name"]: FirecrawlScrapeExecutor,
    KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA["function"]["name"]: KnowledgeBaseQueryExecutor,
}


def get_registered_tool_names():
    registered_tool_names = []
    for schema in ALL_TOOLS_SCHEMAS:
        registered_tool_names.append(schema["function"]["name"])
    registered_tool_names.append(KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA["function"]["name"])
    return registered_tool_names


async def get_tools_for_profile(db: AsyncSession, profile: Profile, embedding_profile_available: bool | None = None) -> tuple[list[dict[str, Any]], list[int]]:
    """
    根据 Profile 动态生成工具列表和可用知识库白名单。
    如果 Profile 没有可用知识库，则不暴露知识库检索工具，也不提供白名单。
    同时，如果知识库可用，为 query_knowledge_base 的 knowledge_base_id 字段动态增加 enum 和描述映射约束。
    """
    base_tools = copy.deepcopy(ALL_TOOLS_SCHEMAS)
    whitelist_ids = []

    try:
        # 嵌入模型不可用（未配置或已禁用）时，知识库检索无法工作，
        # 不向 LLM 暴露知识库查询工具，避免模型调用必然失败的工具。
        is_embedding_available = embedding_profile_available
        if is_embedding_available is None:
            is_embedding_available = await is_embedding_profile_available(db, profile)

        if not is_embedding_available:
            return base_tools, whitelist_ids

        knowledge_bases = await list_available_knowledge_bases(db, profile)
        valid_knowledge_bases = []
        for knowledge_base in knowledge_bases:
            if isinstance(knowledge_base.id, int):
                valid_knowledge_bases.append(knowledge_base)
        if valid_knowledge_bases:
            whitelist_ids = build_knowledge_base_whitelist(valid_knowledge_bases)
            if whitelist_ids:
                # 动态定制知识库查询工具的 Schema
                knowledge_base_tool = copy.deepcopy(KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA)
                # 限制可选的 enum 范围
                parameters = knowledge_base_tool["function"]["parameters"]
                knowledge_base_id_property = parameters["properties"]["knowledge_base_id"]
                knowledge_base_id_property["type"] = "string"
                allowed_knowledge_base_ids = []
                for knowledge_base_id in whitelist_ids:
                    allowed_knowledge_base_ids.append(str(knowledge_base_id))
                knowledge_base_id_property["enum"] = allowed_knowledge_base_ids

                # 动态生成描述信息
                mapping_description = ", ".join(f"ID {knowledge_base.id}: {knowledge_base.name}" for knowledge_base in valid_knowledge_bases)
                knowledge_base_id_property["description"] = f"The id of an allowed knowledge base. Must be one of the ids allowed by the current runtime whitelist. Available options mapping: {mapping_description}."

                base_tools.append(knowledge_base_tool)
    except Exception as e:
        logger.warning(f"Failed to build dynamic tools for profile {getattr(profile, 'id', None)}: {e}")

    return base_tools, whitelist_ids
