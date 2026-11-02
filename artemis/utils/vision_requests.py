"""MiniMax image request checks and structured visual response boundary."""

from __future__ import annotations

import base64
from io import BytesIO

from langchain_core.messages import HumanMessage
from PIL import Image

from artemis.llm.reliability import LLMInvalidOutputError
from artemis.llm.structured import StructuredOutputError
from artemis.services.llm import acomplete_structured


def image_message(data: bytes, prompt: str) -> HumanMessage:
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("MiniMax image exceeds the 10 MB image limit")
    with Image.open(BytesIO(data)) as image:
        mime = Image.MIME.get(image.format, "image/png")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) + len(prompt.encode()) > 64 * 1024 * 1024:
        raise ValueError("MiniMax image request exceeds 64 MB")
    return HumanMessage(
        content=[
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            {"type": "text", "text": prompt},
        ]
    )


async def complete_visual(llm, data: bytes, prompt: str, schema):
    try:
        return await acomplete_structured(llm, [image_message(data, prompt)], schema=schema)
    except StructuredOutputError as error:
        raise LLMInvalidOutputError("MiniMax visual response did not satisfy its schema") from error
