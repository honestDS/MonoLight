from .shell import *

# Tool Registry
ALL_TOOLS_SCHEMAS = [
    SHELL_TOOL_SCHEMA,
]

# Tool Executor Mapping
TOOL_EXECUTOR_MAP = {
    SHELL_TOOL_SCHEMA["function"]["name"]: ShellExecutor,
}

def get_registered_tool_names():
    return [schema["function"]["name"] for schema in ALL_TOOLS_SCHEMAS]
