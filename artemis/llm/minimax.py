"""MiniMax protocol gates shared by Messages and native Files/ASR clients.

Keep transport and block conversion in the official SDK. This module only
normalizes documented errors and validates final messages, without rewriting
opaque thinking signatures, server blocks or assistant history.
https://platform.minimax.io/docs/api-reference/errorcode
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from artemis.llm.reliability import (
    Failure,
    FailureCategory,
    LLMCallError,
    LLMInvalidOutputError,
    classify_failure,
)


class MiniMaxProviderError(Exception):
    """Provider failure awaiting the canonical retry owner (not terminal yet)."""

    def __init__(
        self,
        failure: Failure,
        *,
        code: int | str | None = None,
        trace_id: str | None = None,
        response: Any = None,
    ):
        self.failure, self.code, self.trace_id, self.response = failure, code, trace_id, response
        suffix = f" (trace_id={trace_id})" if trace_id else ""
        super().__init__(f"MiniMax {failure.category.value} error {code or 'unknown'}{suffix}")


_CODES = {
    1008: (FailureCategory.BALANCE, False),
    1004: (FailureCategory.AUTHENTICATION, False),
    2049: (FailureCategory.AUTHENTICATION, False),
    2013: (FailureCategory.BAD_REQUEST, False),
    2022: (FailureCategory.PERMISSION, False),
    1026: (FailureCategory.CONTENT_REFUSAL, False),
    1027: (FailureCategory.CONTENT_REFUSAL, False),
    2061: (FailureCategory.PERMISSION, False),
    2068: (FailureCategory.PERMISSION, False),
    2067: (FailureCategory.QUOTA_EXHAUSTED, False),
    1001: (FailureCategory.TIMEOUT, True),
    1002: (FailureCategory.RATE_LIMIT, True),
    1024: (FailureCategory.PROVIDER_UNAVAILABLE, True),
    1033: (FailureCategory.PROVIDER_UNAVAILABLE, True),
    2064: (FailureCategory.PROVIDER_UNAVAILABLE, True),
    2066: (FailureCategory.PROVIDER_UNAVAILABLE, True),
}
_TYPES = {
    "bad_request_error": FailureCategory.BAD_REQUEST,
    "authorized_error": FailureCategory.AUTHENTICATION,
    "insufficient_balance_error": FailureCategory.BALANCE,
    "unprocessable_entity_error": FailureCategory.CONTENT_REFUSAL,
    "server_error": FailureCategory.PROVIDER_UNAVAILABLE,
    "authentication_error": FailureCategory.AUTHENTICATION,
    "permission_error": FailureCategory.PERMISSION,
    "invalid_request_error": FailureCategory.BAD_REQUEST,
    "rate_limit_error": FailureCategory.RATE_LIMIT,
    "overloaded_error": FailureCategory.PROVIDER_UNAVAILABLE,
    "api_error": FailureCategory.PROVIDER_UNAVAILABLE,
}


def normalize_minimax_error(
    payload: Any = None, *, status_code: int | None = None, error: BaseException | None = None
) -> MiniMaxProviderError | None:
    """Inspect native base_resp, Messages HTTP/SSE errors; never echo secret bodies.

    HTTP-200 native responses may report business errors. This does not assume
    Messages uses base_resp. Unknown business codes remain bounded failures.
    """
    if isinstance(error, (MiniMaxProviderError, LLMCallError)):
        return error if isinstance(error, MiniMaxProviderError) else None
    response = getattr(error, "response", None)
    if payload is None and error is not None:
        payload = getattr(error, "body", None)
        if payload is None and response is not None:
            try:
                payload = response.json()
            except (ValueError, AttributeError):
                pass
    status_code = status_code or getattr(error, "status_code", None)
    data = payload if isinstance(payload, dict) else {}
    details = data.get("base_resp") or data.get("error") or {}
    details = details if isinstance(details, dict) else {}
    code = details.get("status_code", details.get("code"))
    trace_id = data.get("trace_id") or data.get("request_id") or details.get("trace_id")
    if trace_id is not None:
        trace_id = str(trace_id)[:128]
    try:
        numeric = int(code) if code is not None else None
    except (ValueError, TypeError):
        numeric = None
    status_categories = {
        401: FailureCategory.AUTHENTICATION,
        403: FailureCategory.PERMISSION,
        402: FailureCategory.BALANCE,
        422: FailureCategory.CONTENT_REFUSAL,
        429: FailureCategory.RATE_LIMIT,
    }
    http_category = status_categories.get(
        status_code,
        FailureCategory.PROVIDER_UNAVAILABLE
        if status_code is not None and status_code >= 500
        else FailureCategory.BAD_REQUEST,
    )
    if numeric not in (None, 0):
        if numeric in _CODES:
            category, retryable = _CODES[numeric]
        else:
            category = _TYPES.get(
                details.get("type"),
                http_category
                if status_code is not None and status_code >= 400
                else FailureCategory.UNKNOWN,
            )
            retryable = category in {
                FailureCategory.RATE_LIMIT,
                FailureCategory.PROVIDER_UNAVAILABLE,
                FailureCategory.UNKNOWN,
            }
    elif data.get("type") == "error" or data.get("error"):
        category = _TYPES.get(
            details.get("type"),
            http_category
            if status_code is not None and status_code >= 400
            else FailureCategory.UNKNOWN,
        )
        retryable = category in {
            FailureCategory.RATE_LIMIT,
            FailureCategory.PROVIDER_UNAVAILABLE,
            FailureCategory.UNKNOWN,
        }
    elif status_code is not None and status_code >= 400:
        category = http_category
        retryable = category in {FailureCategory.PROVIDER_UNAVAILABLE, FailureCategory.RATE_LIMIT}
    elif error is not None:
        failure = classify_failure(error)
        if failure.category is FailureCategory.CANCELLED:
            return None
        return MiniMaxProviderError(failure, trace_id=trace_id, response=response)
    else:
        return None
    return MiniMaxProviderError(
        Failure(category, retryable, retryable),
        code=numeric or code or status_code,
        trace_id=trace_id,
        response=response,
    )


def raise_for_minimax_error(payload: Any, *, status_code: int | None = None) -> None:
    """Native clients call this before accepting an HTTP/SSE payload as success."""
    failure = normalize_minimax_error(payload, status_code=status_code)
    if failure is not None:
        raise failure


def minimax_wire_target(endpoint: Any) -> str:
    """Ignore timeout/effort when preventing a second retry round on the same wire target."""
    identity = (str(endpoint.api_base).rstrip("/"), endpoint.model_name, endpoint.api_key)
    return "minimax:" + hashlib.sha256(repr(identity).encode()).hexdigest()[:24]


def _complete_tool_input(block: dict) -> dict:
    """Validate SDK stream arguments before its history formatter can discard them."""
    original = block.get("input")
    value = original
    if "partial_json" in block:
        try:
            value = json.loads(block["partial_json"])
        except (TypeError, ValueError) as error:
            raise LLMInvalidOutputError(
                "MiniMax returned incomplete streamed tool arguments"
            ) from error
        if original not in ({}, value):
            raise LLMInvalidOutputError("MiniMax streamed tool arguments disagree with input")
    if not isinstance(original, dict) or not isinstance(value, dict):
        raise LLMInvalidOutputError("MiniMax tool arguments must be complete JSON objects")
    try:
        # json.loads accepts NaN/Infinity and overflowing exponents by default.
        # A finite JSON object is required for a lossless SDK history roundtrip.
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        raise LLMInvalidOutputError(
            "MiniMax tool arguments must contain finite JSON values"
        ) from error
    return value


def validate_minimax_response(
    response: Any, *, client_tools: frozenset[str] = frozenset(), allow_pause_turn: bool = False
) -> Any:
    """Return the exact original raw message, or fail before parse/action dispatch."""
    metadata = getattr(response, "response_metadata", {}) or {}
    reason = metadata.get("stop_reason")
    permitted = {"end_turn", "stop_sequence", "tool_use"}
    if allow_pause_turn:
        permitted.add("pause_turn")
    if reason not in permitted:
        raise LLMInvalidOutputError(f"MiniMax invalid terminal state: {reason or 'missing'}")
    if getattr(response, "invalid_tool_calls", None):
        raise LLMInvalidOutputError("MiniMax returned invalid client tool calls")
    content = getattr(response, "content", None)
    calls = getattr(response, "tool_calls", None) or []
    blocks = content if isinstance(content, list) else []
    inputs = {
        id(block): _complete_tool_input(block)
        for block in blocks
        if isinstance(block, dict) and block.get("type") in {"tool_use", "server_tool_use"}
    }
    local_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    ids = set()
    for call in calls:
        call_id = call.get("id")
        if (
            not call_id
            or call_id in ids
            or call.get("name") not in client_tools
            or not isinstance(call.get("args"), dict)
        ):
            raise LLMInvalidOutputError(
                "MiniMax returned an unregistered or malformed client tool call"
            )
        ids.add(call_id)
    if local_blocks and len(local_blocks) != len(calls):
        raise LLMInvalidOutputError("MiniMax client tool blocks could not be decoded")
    for block, call in zip(local_blocks, calls):
        block_input = inputs[id(block)]
        if (
            block.get("id") != call.get("id")
            or block.get("name") != call.get("name")
            or block_input != call.get("args")
        ):
            raise LLMInvalidOutputError("MiniMax client tool identity/arguments mismatch")
    if (reason == "tool_use") != bool(calls):
        raise LLMInvalidOutputError("MiniMax stop reason and client tool calls disagree")
    text = (
        content
        if isinstance(content, str)
        else "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
    )
    server_result = allow_pause_turn and any(
        isinstance(b, dict) and b.get("type") == "web_search_tool_result" for b in blocks
    )
    if not text.strip() and not calls and reason != "pause_turn" and not server_result:
        raise LLMInvalidOutputError("MiniMax returned only thinking or empty output")
    if reason == "pause_turn" and not any(
        isinstance(b, dict) and b.get("type") in {"server_tool_use", "web_search_tool_result"}
        for b in blocks
    ):
        raise LLMInvalidOutputError("MiniMax paused without server search content")
    return response
