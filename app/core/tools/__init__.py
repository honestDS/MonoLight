import copy
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.channel_router import select_channel
from app.core.embedding.knowledge_base import build_knowledge_base_whitelist, list_available_knowledge_bases
from app.core.log import get_logger
from app.models.channel import ChannelConfig
from app.models.profile import Profile

from .cancel_background_task import CANCEL_BACKGROUND_TASK_TOOL_SCHEMA, CancelBackgroundTaskExecutor
from .file_writer import FILE_WRITER_TOOL_SCHEMA, FileWriterExecutor
from .firecrawl_scrape import FIRECRAWL_SCRAPE_TOOL_SCHEMA, FirecrawlScrapeExecutor
from .firecrawl_search import FIRECRAWL_SEARCH_TOOL_SCHEMA, FirecrawlSearchExecutor
from .image_generation import IMAGE_GENERATION_TOOL_SCHEMA, ImageGenerationExecutor
from .knowledge_base_query import KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA, KnowledgeBaseQueryExecutor
from .list_background_tasks import LIST_BACKGROUND_TASKS_TOOL_SCHEMA, ListBackgroundTasksExecutor
from .send_file_to_user import SEND_FILE_TO_USER_TOOL_SCHEMA, SendFileToUserExecutor
from .shell import SHELL_TOOL_SCHEMA, ShellExecutor

logger = get_logger(__name__)

# Tool Registry
CONFIGURABLE_TOOL_SCHEMAS = [
    SHELL_TOOL_SCHEMA,
    FILE_WRITER_TOOL_SCHEMA,
    FIRECRAWL_SEARCH_TOOL_SCHEMA,
    FIRECRAWL_SCRAPE_TOOL_SCHEMA,
    SEND_FILE_TO_USER_TOOL_SCHEMA,
    LIST_BACKGROUND_TASKS_TOOL_SCHEMA,
    CANCEL_BACKGROUND_TASK_TOOL_SCHEMA,
]

CONFIGURABLE_CONDITIONAL_TOOL_SCHEMAS = [
    IMAGE_GENERATION_TOOL_SCHEMA,
]

CONFIGURABLE_DYNAMIC_TOOL_SCHEMAS = [
    KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA,
]

BACKGROUND_CAPABLE_TOOL_NAMES = {
    FIRECRAWL_SEARCH_TOOL_SCHEMA["function"]["name"],
    FIRECRAWL_SCRAPE_TOOL_SCHEMA["function"]["name"],
    IMAGE_GENERATION_TOOL_SCHEMA["function"]["name"],
}

ALL_TOOLS_SCHEMAS = CONFIGURABLE_TOOL_SCHEMAS

# Tool Executor Mapping
TOOL_EXECUTOR_MAP = {
    SHELL_TOOL_SCHEMA["function"]["name"]: ShellExecutor,
    FILE_WRITER_TOOL_SCHEMA["function"]["name"]: FileWriterExecutor,
    FIRECRAWL_SEARCH_TOOL_SCHEMA["function"]["name"]: FirecrawlSearchExecutor,
    FIRECRAWL_SCRAPE_TOOL_SCHEMA["function"]["name"]: FirecrawlScrapeExecutor,
    SEND_FILE_TO_USER_TOOL_SCHEMA["function"]["name"]: SendFileToUserExecutor,
    LIST_BACKGROUND_TASKS_TOOL_SCHEMA["function"]["name"]: ListBackgroundTasksExecutor,
    CANCEL_BACKGROUND_TASK_TOOL_SCHEMA["function"]["name"]: CancelBackgroundTaskExecutor,
    IMAGE_GENERATION_TOOL_SCHEMA["function"]["name"]: ImageGenerationExecutor,
    KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA["function"]["name"]: KnowledgeBaseQueryExecutor,
}


def get_registered_tool_names():
    registered_tool_names = []
    for schema in CONFIGURABLE_TOOL_SCHEMAS:
        registered_tool_names.append(schema["function"]["name"])
    for schema in CONFIGURABLE_CONDITIONAL_TOOL_SCHEMAS:
        registered_tool_names.append(schema["function"]["name"])
    for schema in CONFIGURABLE_DYNAMIC_TOOL_SCHEMAS:
        registered_tool_names.append(schema["function"]["name"])
    return registered_tool_names


def _inject_background_control(schema: dict[str, Any]) -> dict[str, Any]:
    tool_name = schema["function"]["name"]
    if tool_name not in BACKGROUND_CAPABLE_TOOL_NAMES:
        return schema

    parameters = schema["function"].setdefault("parameters", {})
    properties = parameters.setdefault("properties", {})
    properties.setdefault(
        "run_in_background",
        {
            "type": "boolean",
            "description": "Set true for long-running work that should continue in the background. The system will notify the user after completion.",
            "default": False,
        },
    )
    return schema


def _iter_tool_schemas() -> list[dict[str, Any]]:
    return [*CONFIGURABLE_TOOL_SCHEMAS, *CONFIGURABLE_CONDITIONAL_TOOL_SCHEMAS, *CONFIGURABLE_DYNAMIC_TOOL_SCHEMAS]


def tool_schema_has_parameter(tool_name: str, parameter_name: str) -> bool:
    for schema in _iter_tool_schemas():
        if schema["function"]["name"] != tool_name:
            continue
        tool_schema = _inject_background_control(copy.deepcopy(schema))
        properties = tool_schema.get("function", {}).get("parameters", {}).get("properties", {})
        return isinstance(properties, dict) and parameter_name in properties
    return False


def _get_enabled_tool_names(profile: Profile) -> set[str]:
    configs = profile.configs or {}
    tool_config = configs.get("tool", {}) if isinstance(configs, dict) else {}
    enabled_tools = tool_config.get("enabled_tools")
    if enabled_tools is None:
        return set(get_registered_tool_names())
    if not isinstance(enabled_tools, list):
        return set()
    return {name for name in enabled_tools if isinstance(name, str)}


async def _is_image_generation_profile_available(db: AsyncSession, profile: Profile) -> bool:
    configs = profile.configs or {}
    if not isinstance(configs, dict):
        return False

    channel_config = configs.get("channel", {})
    if not isinstance(channel_config, dict):
        return False

    image_generation_channel_raw = channel_config.get("image_generation_channel")
    if not image_generation_channel_raw:
        return False

    try:
        image_generation_channel = ChannelConfig.model_validate(image_generation_channel_raw)
    except Exception:
        return False

    if not image_generation_channel.rules:
        return False

    selected_channel = await select_channel(
        db,
        image_generation_channel,
        "IMAGE_GENERATION",
        excluded_priorities=set(),
        cursor_key=None,
        log_selection=False,
    )
    return selected_channel is not None


async def get_tools_for_profile(db: AsyncSession, profile: Profile, *, allow_background: bool = True) -> tuple[list[dict[str, Any]], list[int]]:
    """
    根据 Profile 中的 enabled_tools 生成当前会话向 LLM 暴露的工具列表。
    query_knowledge_base 属于动态工具：只有被启用且存在可用知识库时才暴露，并会注入运行时知识库白名单。
    """
    enabled_tool_names = _get_enabled_tool_names(profile)
    base_tools = []
    for schema in CONFIGURABLE_TOOL_SCHEMAS:
        if schema["function"]["name"] in enabled_tool_names:
            tool_schema = copy.deepcopy(schema)
            base_tools.append(_inject_background_control(tool_schema) if allow_background else tool_schema)
    if IMAGE_GENERATION_TOOL_SCHEMA["function"]["name"] in enabled_tool_names and await _is_image_generation_profile_available(db, profile):
        image_tool_schema = copy.deepcopy(IMAGE_GENERATION_TOOL_SCHEMA)
        base_tools.append(_inject_background_control(image_tool_schema) if allow_background else image_tool_schema)
    whitelist_ids = []

    try:
        if KNOWLEDGE_BASE_QUERY_TOOL_SCHEMA["function"]["name"] not in enabled_tool_names:
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
