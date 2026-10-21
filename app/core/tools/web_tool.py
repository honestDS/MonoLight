import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.constants import ERR_TOOL_ARGUMENT_SCHEMA_INVALID, WEB_TOOL_NAME
from app.core.i18n import t
from app.core.utils.system import get_full_system_context
from app.providers.web import get_web_client

from .base import BaseExecutor


class _WebSearchArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str


class _WebScrapeArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    url: str
    formats: list[Literal["markdown", "html", "raw_html", "screenshot", "links"]] = Field(default_factory=lambda: ["markdown"])
    only_main_content: bool = True
    mobile: bool = False
    wait_for: int = Field(default=0, ge=0)


def _argument_error(operation: str, detail: str) -> str:
    error = t(ERR_TOOL_ARGUMENT_SCHEMA_INVALID, tool_name=WEB_TOOL_NAME, detail=detail)
    return json.dumps({"status": "failed", "operation": operation, "error": error}, ensure_ascii=False, default=str)


class WebToolExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, operation: str, **arguments) -> str:
        argument_model = {
            "search": _WebSearchArguments,
            "scrape": _WebScrapeArguments,
        }.get(operation)
        if argument_model is None:
            detail = json.dumps(
                {"operation": operation, "allowed_operations": ["search", "scrape"]},
                ensure_ascii=False,
                default=str,
            )
            return _argument_error(operation, detail)

        try:
            validated_arguments = argument_model.model_validate(arguments)
        except ValidationError as exc:
            detail = json.dumps(
                exc.errors(include_url=False, include_context=False, include_input=False),
                ensure_ascii=False,
            )
            return _argument_error(operation, detail)

        system_info = get_full_system_context()
        try:
            client = get_web_client(self.cfg.tool if self.cfg else None, uid=self.uid)
            if operation == "search":
                results = await client.search(**validated_arguments.model_dump())
                return json.dumps(results, ensure_ascii=False, default=str)

            url = validated_arguments.url
            data = await client.scrape(**validated_arguments.model_dump())
            return json.dumps(
                {"status": "success", "url": url, "data": data, "system_info": system_info},
                ensure_ascii=False,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc), "system_info": system_info}, ensure_ascii=False)


WEB_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": WEB_TOOL_NAME,
        "description": "Search the web for up to 10 results or scrape a web page; the backend selects the web service.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["search", "scrape"],
                    "description": "Select whether to search the web or scrape a page.",
                },
                "query": {
                    "type": "string",
                    "description": "Required for search; the query returns up to 10 results.",
                },
                "url": {
                    "type": "string",
                    "description": "Required for scrape; the URL of the page to scrape.",
                },
                "formats": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["markdown", "html", "raw_html", "screenshot", "links"],
                    },
                    "description": "Scrape only; formats to return from the page.",
                    "default": ["markdown"],
                },
                "only_main_content": {
                    "type": "boolean",
                    "description": "Scrape only; return only the page's main content.",
                    "default": True,
                },
                "mobile": {
                    "type": "boolean",
                    "description": "Scrape only; emulate a mobile device.",
                    "default": False,
                },
                "wait_for": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Scrape only; milliseconds to wait before scraping.",
                    "default": 0,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}
