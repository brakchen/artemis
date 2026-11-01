"""Isolated MiniMax Messages server search; no device state or caller history."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from artemis.llm.reliability import LLMCallError, FailureCategory, logical_call_scope
from artemis.llm.structured import content_to_text


SERVER_SEARCH = {"type": "web_search_20250305", "name": "web_search"}


class SearchSource(BaseModel):
    url: str
    title: str = ""


class SearchResult(BaseModel):
    status: Literal["complete", "incomplete", "unavailable", "error"]
    answer: str = ""
    sources: list[SearchSource] = Field(default_factory=list)
    error: str | None = None


def _extract_sources(content) -> tuple[list[SearchSource], str | None]:
    sources = {}
    error = None

    def add_source(result):
        url = result.get("url")
        if not isinstance(url, str):
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        title = result.get("title")
        sources[url] = SearchSource(url=url, title=title if isinstance(title, str) else "")
        return True

    # MiniMax's server-tools example uses content; Anthropic-compatible
    # results use encrypted_content. Preserve either body untouched:
    # https://platform.minimax.cn/docs/guides/server-tools
    # Also accept SDK CitationsWebSearchResultLocation inside text blocks.
    # Never collect URL-shaped fields from inputs or opaque/unknown blocks.
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "web_search_tool_result":
            results = block.get("content")
            if isinstance(results, dict) and results.get("type") == "web_search_tool_result_error":
                error = (
                    "MiniMax server search unavailable: "
                    + str(results.get("error_code", "unknown"))[:80]
                )
            elif isinstance(results, list):
                for result in results:
                    if not (
                        isinstance(result, dict)
                        and result.get("type") == "web_search_result"
                        and isinstance(result.get("title"), str)
                        and (
                            isinstance(result.get("content"), str)
                            or isinstance(result.get("encrypted_content"), str)
                        )
                        and add_source(result)
                    ):
                        error = (
                            "MiniMax returned malformed search results: invalid search result entry"
                        )
            else:
                error = "MiniMax returned malformed search results: expected a result list or search error"
        elif block.get("type") == "text" and isinstance(block.get("citations"), list):
            for citation in block["citations"]:
                if (
                    isinstance(citation, dict)
                    and citation.get("type") == "web_search_result_location"
                ):
                    add_source(citation)
    return list(sources.values()), error


async def web_search(
    ctx, query: str, *, role: str = "default", max_continuations: int = 2
) -> SearchResult:
    """Search using the pinned default endpoint, with at most two exact continuations.

    Only the supplied query enters the independent Messages session. Native
    server blocks never enter a local tool executor. Beta/plan permission
    failures are explicit; no other search provider is substituted.
    """
    from artemis.services.llm import RobustChatModelWrapper, get_effective_endpoint
    from artemis.llm.router import ModelFactory

    endpoint = get_effective_endpoint(ctx, "default")
    policy_endpoint = get_effective_endpoint(ctx, role) if role != "default" else endpoint
    if (
        endpoint.vendor != "minimax"
        or policy_endpoint.vendor != "minimax"
        or not policy_endpoint.enable_grounding
    ):
        return SearchResult(status="unavailable", error="MiniMax web search is not enabled")
    if not isinstance(query, str) or not query.strip():
        return SearchResult(status="error", error="Search query must not be empty")
    llm = RobustChatModelWrapper(
        ModelFactory.get_model(endpoint), ctx, endpoint=endpoint, allow_pause_turn=True
    ).bind_tools([SERVER_SEARCH])
    messages = [HumanMessage(content=query)]
    answers, sources = [], {}
    search_performed = False
    limit = min(2, max(0, max_continuations))
    with logical_call_scope():
        for index in range(limit + 1):
            try:
                response = await llm.ainvoke(messages)
            except LLMCallError as error:
                status = (
                    "unavailable"
                    if error.failure.category
                    in {
                        FailureCategory.PERMISSION,
                        FailureCategory.AUTHENTICATION,
                    }
                    else "error"
                )
                return SearchResult(
                    status=status,
                    answer="\n".join(answers),
                    sources=list(sources.values()),
                    error=str(error),
                )
            text = content_to_text(response.content)
            if text:
                answers.append(text)
            actual_sources, server_error = _extract_sources(response.content)
            search_performed = search_performed or any(
                isinstance(block, dict) and block.get("type") == "web_search_tool_result"
                for block in response.content
                if isinstance(response.content, list)
            )
            sources.update((s.url, s) for s in actual_sources)
            if server_error:
                return SearchResult(
                    status="unavailable"
                    if server_error.startswith("MiniMax server search unavailable:")
                    else "error",
                    answer="\n".join(answers),
                    sources=list(sources.values()),
                    error=server_error,
                )
            if response.response_metadata.get("stop_reason") != "pause_turn":
                return SearchResult(
                    status="complete" if search_performed else "incomplete",
                    answer="\n".join(answers),
                    sources=list(sources.values()),
                    error=None
                    if search_performed
                    else "MiniMax returned no server search evidence",
                )
            if index < limit:
                # Keep every block, signature, ID and order. A fabricated user
                # "continue" turn would lose the server-tool continuation state.
                messages = [*messages, response]
    return SearchResult(
        status="incomplete",
        answer="\n".join(answers),
        sources=list(sources.values()),
        error="Search continuation budget exhausted",
    )
