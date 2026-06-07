from .file_writer import FILE_WRITER_TOOL_SCHEMA, FileWriterExecutor
from .firecrawl_scrape import FIRECRAWL_SCRAPE_TOOL_SCHEMA, FirecrawlScrapeExecutor
from .firecrawl_search import FIRECRAWL_SEARCH_TOOL_SCHEMA, FirecrawlSearchExecutor
from .shell import SHELL_TOOL_SCHEMA, ShellExecutor

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
}


def get_registered_tool_names():
    return [schema["function"]["name"] for schema in ALL_TOOLS_SCHEMAS]
