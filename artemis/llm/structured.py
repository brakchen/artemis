# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unified structured-output parsing for LLM responses.

Single pipeline replacing the ad-hoc JSON extraction scattered across agents:
strip thinking tags → extract the JSON payload (fenced or balanced-span) →
parse → tolerant repair pass → optional schema validation.  A failed parse
returns a typed ``ParseFailure`` carrying the raw text and the reason — never
a raw string masquerading as parsed data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from jsonschema.validators import Draft202012Validator, validator_for
from langchain_core.messages import SystemMessage
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import specification_with

from artemis.llm.parser import extract_thinking_tags


@dataclass(frozen=True)
class ParseFailure:
    """A parse miss: keeps the raw text and the reason, explicitly typed."""

    raw: str
    error: str


class StructuredOutputError(ValueError):
    """Raised when a required structured response could not be parsed."""

    def __init__(self, failure: ParseFailure):
        super().__init__(failure.error)
        self.failure = failure


def content_to_text(content: Any) -> str:
    """Flatten a LangChain message content (str or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") != "thinking":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _balanced_span(text: str) -> str | None:
    """Return the first balanced {...} or [...] span, string/escape aware."""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or (stack[-1], ch) not in {("{", "}"), ("[", "]")}:
                return None
            stack.pop()
            if not stack:
                return text[start : j + 1]
    return None


def extract_json_candidate(text: str) -> str | None:
    """Locate the JSON payload inside prose: fenced blocks first, then bare spans."""
    text = text.strip()
    for match in _FENCE_RE.finditer(text):
        span = _balanced_span(match.group(1).strip())
        if span:
            return span
    return _balanced_span(text)


def repair_json(text: str) -> str:
    """Remove comment and trailing-comma noise outside of strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ",":
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] in "]}":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_schema(schema: Any) -> dict | None:
    """Preflight supported schemas locally; never resolve remote references."""
    if schema is None:
        return None
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        rendered = schema.model_json_schema()
    elif isinstance(schema, dict):
        rendered = schema
    else:
        raise ValueError("Structured output requires a Pydantic model or JSON Schema dict")
    json.dumps(rendered, allow_nan=False)
    validator = validator_for(
        rendered, default=Draft202012Validator if "$schema" not in rendered else None
    )
    if validator is None:
        raise ValueError("Unsupported JSON Schema dialect")
    validator.check_schema(rendered)

    # referencing 0.37.0 (MIT), the public resolver used by jsonschema:
    # https://referencing.readthedocs.io/en/stable/api/
    # Resource.subresources follows schema positions for the selected dialect,
    # preserving literal const/default values and property names such as "$ref".
    dialect = validator.META_SCHEMA.get("$id") or validator.META_SCHEMA["id"]
    root = Resource.from_contents(rendered, default_specification=specification_with(dialect))
    resolver = Registry().resolver_with_root(root)  # default registry never retrieves remotely

    def preflight(resource, scope):
        value = resource.contents
        if isinstance(value, dict):
            if "$dynamicRef" in value or "$recursiveRef" in value:
                raise ValueError("Dynamic and recursive JSON Schema references are not supported")
            if "$ref" in value:
                try:
                    scope.lookup(value["$ref"])
                except Unresolvable as error:
                    raise ValueError(
                        "JSON Schema reference is not resolvable within the supplied schema"
                    ) from error
        for child in resource.subresources():
            preflight(child, scope.in_subresource(child))

    preflight(root, resolver)
    return rendered


def structured_messages(messages: list, schema: Any) -> list:
    """Merge JSON instructions into one system message from the first request."""
    rendered = validate_schema(schema)
    constraints = (
        "Return ONLY one valid JSON value. No prose, Markdown fences or comments. "
        "Do not use tools to submit the JSON. The JSON must satisfy this JSON Schema:\n"
        + json.dumps(rendered or {}, ensure_ascii=False)
    )
    system_blocks = []
    other = []
    for message in messages:
        if isinstance(message, SystemMessage):
            system_blocks.extend(
                message.content
                if isinstance(message.content, list)
                else [{"type": "text", "text": message.content}]
            )
        else:
            other.append(message)
    system_blocks.append({"type": "text", "text": constraints})
    return [SystemMessage(content=system_blocks), *other]


def parse_structured(text: Any, schema: Any = None, *, strict: bool = False) -> Any | ParseFailure:
    """Parse an LLM response into JSON data (optionally schema-validated).

    Returns the parsed object (or a validated ``schema`` instance), or a
    ``ParseFailure`` describing why the text could not be parsed. Callers must
    check ``isinstance(result, ParseFailure)`` — the raw text is never
    silently passed through as a result.
    """
    rendered = validate_schema(schema)
    if not isinstance(text, str):
        text = content_to_text(text)
    if not text or not text.strip():
        return ParseFailure(raw=text or "", error="empty response")

    cleaned, _ = extract_thinking_tags(text)
    candidate = cleaned.strip() if strict else extract_json_candidate(cleaned)
    if not strict and candidate is None:
        # JSON Schema also permits scalar values (including null).
        try:
            json.loads(cleaned.strip())
        except (ValueError, TypeError):
            pass
        else:
            candidate = cleaned.strip()
    if candidate is None:
        return ParseFailure(raw=text, error="no JSON object or array found in response")

    last_error = ""
    for variant in (candidate,) if strict else (candidate, repair_json(candidate)):
        try:

            def reject_constant(value):
                raise ValueError(f"Non-JSON numeric constant: {value}")

            data = (
                json.loads(variant, parse_constant=reject_constant)
                if strict
                else json.loads(variant)
            )
        except (json.JSONDecodeError, ValueError) as decode_error:
            last_error = str(decode_error)
            continue
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                return schema.model_validate(data)
            except ValidationError as validation_error:
                return ParseFailure(raw=text, error=f"schema validation failed: {validation_error}")
        if rendered is not None:
            try:
                validator_for(rendered)(rendered, format_checker=FormatChecker()).validate(data)
            except JSONSchemaValidationError as validation_error:
                return ParseFailure(
                    raw=text, error=f"schema validation failed: {validation_error.message}"
                )
        return data
    return ParseFailure(raw=text, error=f"invalid JSON: {last_error}")
