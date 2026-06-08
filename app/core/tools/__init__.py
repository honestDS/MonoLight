import copy
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.embedding.knowledge_base import build_knowledge_base_whitelist, list_available_knowledge_bases
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
    return [schema["function"]["name"] for schema in ALL_TOOLS_SCHEMAS] + [KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA["function"]["name"]]


async def get_tools_for_profile(db: AsyncSession, profile: Profile) -> tuple[list[dict[str, Any]], list[int]]:
    """
    根据 Profile 动态生成工具列表和可用知识库白名单。
    如果 Profile 没有可用知识库，则不暴露知识库检索工具，也不提供白名单。
    同时，如果知识库可用，为 query_knowledge_base 的 knowledge_base_id 字段动态增加 enum 和描述映射约束。
    """
    base_tools = copy.deepcopy(ALL_TOOLS_SCHEMAS)
    whitelist_ids = []

    try:
        kbs = await list_available_knowledge_bases(db, profile)
        valid_kbs = [kb for kb in kbs if isinstance(kb.id, int)]
        if valid_kbs:
            whitelist_ids = build_knowledge_base_whitelist(valid_kbs)
            if whitelist_ids:
                # 动态定制知识库查询工具的 Schema
                kb_tool = copy.deepcopy(KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA)
                # 限制可选的 enum 范围
                parameters = kb_tool["function"]["parameters"]
                kb_id_prop = parameters["properties"]["knowledge_base_id"]
                kb_id_prop["type"] = "string"
                kb_id_prop["enum"] = [str(kb_id) for kb_id in whitelist_ids]

                # 动态生成描述信息
                mapping_desc = ", ".join([f"ID {kb.id}: {kb.name}" for kb in valid_kbs])
                kb_id_prop["description"] = f"The id of an allowed knowledge base. Must be one of the ids allowed by the current runtime whitelist. Available options mapping: {mapping_desc}."

                base_tools.append(kb_tool)
    except Exception as e:
        logger.warning(f"Failed to build dynamic tools for profile {getattr(profile, 'id', None)}: {e}")

    return base_tools, whitelist_ids
