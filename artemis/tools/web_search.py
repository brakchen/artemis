"""Ordinary query-only Agent tool for the configured MiniMax search feature."""

from __future__ import annotations

from pydantic import BaseModel, Field

from artemis.tools.base import ArtemisTool
from artemis.core.tool_failure import ToolFailure

SEARCH_TOOL_GUIDANCE = (
    "Use `web_search` for current public facts or unfamiliar error codes. Send only a public query, "
    "never device history or private content. Cite returned source URLs and distinguish "
    "complete evidence from incomplete, unavailable, or failed search."
)


class WebSearchArgs(BaseModel):
    query: str = Field(
        min_length=1, description="Public web query; include no device history or secrets"
    )


async def _search(ctx, query: str, *, role: str = "default") -> str:
    from artemis.services.web_search import web_search

    result = await web_search(ctx, query, role=role)
    payload = result.model_dump_json()
    return payload if result.status == "complete" else ToolFailure(payload)


WEB_SEARCH_TOOL = ArtemisTool(
    name="web_search",
    description="Search the public web for current facts or unfamiliar errors. Supply only a public query. Returns an answer, actual source URLs/titles, and complete/incomplete/unavailable/error status. Treat unavailable or incomplete evidence explicitly.",
    args_schema=WebSearchArgs,
    handler=_search,
    category="system",
)


def web_search_enabled(ctx, role: str) -> bool:
    from artemis.services.llm import get_effective_endpoint

    if getattr(ctx, "llm_config", None) is None and not isinstance(
        getattr(ctx, "_llm_endpoint_snapshot", None), dict
    ):
        return False
    if role in {"flash", "validator"}:
        role = "operator"
    try:
        endpoint = get_effective_endpoint(ctx, role)
    except (ValueError, AttributeError, TypeError):
        # Optional discovery also runs with legacy/test contexts that do not
        # carry a complete endpoint. The actual model's preflight still owns
        # configuration errors; search must not alter legacy tool discovery.
        return False
    return endpoint.vendor == "minimax" and endpoint.enable_grounding is True


def get_web_search_tools(ctx, role: str) -> list:
    if not web_search_enabled(ctx, role):
        return []
    from langchain_core.tools import StructuredTool

    async def search(query: str) -> str:
        return await _search(ctx, query, role=role)

    return [
        StructuredTool.from_function(
            coroutine=search,
            name="web_search",
            description=WEB_SEARCH_TOOL.description,
            args_schema=WebSearchArgs,
        )
    ]
