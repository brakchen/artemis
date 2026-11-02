"""MiniMax input bounds at the SDK's final Messages payload boundary.

The private hook is pinned to langchain-anthropic 1.5.6 (MIT):
https://github.com/langchain-ai/langchain/tree/master/libs/partners/anthropic
The upstream formatter/Anthropic SDK retain ownership of all block conversion,
thinking signatures, tool history and HTTP serialization. No media is uploaded
to Files. Remote URL sizes are unknown locally and remain server validated.
"""

from __future__ import annotations

import base64
import binascii
import json

from langchain_anthropic import ChatAnthropic

from artemis.llm.minimax import MiniMaxProviderError
from artemis.llm.reliability import Failure, FailureCategory

IMAGE_LIMIT = 10 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024
MESSAGES_LIMIT = 64 * 1024 * 1024


def media_input_error(message: str) -> MiniMaxProviderError:
    error = MiniMaxProviderError(Failure(FailureCategory.BAD_REQUEST, False, False), code=413)
    error.args = (message,)
    return error


def validate_media_payload(payload: dict) -> None:
    """Check every inline media block and the complete SDK-formatted body."""

    def content_blocks(blocks):
        if not isinstance(blocks, list):
            return
        for value in blocks:
            if not isinstance(value, dict):
                continue
            kind, source = value.get("type"), value.get("source")
            if kind in {"image", "video"} and isinstance(source, dict):
                if source.get("type") == "base64":
                    try:
                        size = len(base64.b64decode(source.get("data", ""), validate=True))
                    except (ValueError, TypeError, binascii.Error) as exc:
                        raise media_input_error("Invalid inline MiniMax media encoding") from exc
                    limit = IMAGE_LIMIT if kind == "image" else VIDEO_LIMIT
                    if size > limit:
                        raise media_input_error(f"MiniMax {kind} exceeds its {limit}-byte limit")
                elif source.get("type") != "url":
                    raise media_input_error("Unsupported MiniMax media source")
            # Tool results may carry actual media. Tool-use inputs and tool
            # JSON schemas are ordinary JSON even when a literal resembles a
            # native media block; their bytes count only toward the full body.
            if kind == "tool_result":
                content_blocks(value.get("content"))

    for message in payload.get("messages", []):
        if isinstance(message, dict):
            content_blocks(message.get("content"))
    # httpx 0.28.1 JSON encoding (used by anthropic 0.122.0): UTF-8 compact
    # JSON, ensure_ascii=False, allow_nan=False. This measures history/tools/
    # system/options as well as the new media, including base64 expansion.
    size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )
    if size > MESSAGES_LIMIT:
        raise media_input_error("MiniMax complete Messages body exceeds 64 MiB")


class MiniMaxChatAnthropic(ChatAnthropic):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        validate_media_payload(payload)
        return payload
