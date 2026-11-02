"""Real Anthropic SDK protocol fixtures and raw gateway lifecycle regressions."""

from __future__ import annotations

import asyncio
import json
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel
from pydantic import Field

from artemis.llm.minimax import normalize_minimax_error, raise_for_minimax_error
from artemis.llm.reliability import (
    CircuitBreaker,
    FailureCategory,
    LLMExhaustedError,
    LLMInvalidOutputError,
    LLMPermanentError,
    logical_call_scope,
    classify_failure,
)
from artemis.llm.router import ModelEndpoint, ModelFactory
from artemis.llm.structured import StructuredOutputError
from artemis.services import llm as gateway
from artemis.services import web_search as search_service


def endpoint(**kwargs):
    return ModelEndpoint(
        provider="anthropic",
        vendor="minimax",
        model_name="MiniMax-M3.1-Flash",
        api_base="https://fixture.invalid/anthropic",
        api_key="fixture-not-a-real-key",
        enable_grounding=True,
        **kwargs,
    )


def body(content, reason="end_turn"):
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "MiniMax-M3.1-Flash",
        "content": content,
        "stop_reason": reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 8,
            "output_tokens": 5,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
        },
    }


def raw(text="answer", reason="end_turn", **kwargs):
    return AIMessage(content=text, response_metadata={"stop_reason": reason}, **kwargs)


class RawFake:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.tools = None

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    async def astream(self, messages, **kwargs):
        yield await self.ainvoke(messages, **kwargs)


@pytest.fixture(autouse=True)
def isolate_gateway(monkeypatch):
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", CircuitBreaker(threshold=1000))
    monkeypatch.setattr(gateway, "_NON_STREAMING_ENDPOINTS", set())


def sdk_fixture(responder):
    requests = []

    async def handle(request):
        data = json.loads(request.content)
        requests.append(data)
        result = responder(data, len(requests))
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    sdk = anthropic.AsyncAnthropic(
        api_key="fixture-not-a-real-key",
        base_url="https://fixture.invalid/anthropic",
        max_retries=0,
        http_client=client,
    )
    model = ModelFactory.create_model(endpoint())
    model._async_client = sdk
    return gateway.RobustChatModelWrapper(model, endpoint=endpoint()), requests, client


@pytest.mark.asyncio
async def test_sdk_preserves_all_blocks_and_client_tool_history():
    blocks = [
        {"type": "thinking", "thinking": "opaque reasoning", "signature": "signed-fixture"},
        {"type": "text", "text": "checking"},
        {
            "type": "server_tool_use",
            "id": "srv_1",
            "name": "web_search",
            "input": {"query": "fact"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srv_1",
            "content": [
                {
                    "type": "web_search_result",
                    "url": "https://source.invalid/page",
                    "title": "Source",
                    "encrypted_content": "opaque-result",
                    "page_age": None,
                }
            ],
        },
        {"type": "tool_use", "id": "local_1", "name": "lookup", "input": {"query": "fact"}},
    ]
    wrapper, requests, client = sdk_fixture(
        lambda _, n: (
            body(blocks, "tool_use") if n == 1 else body([{"type": "text", "text": "done"}])
        )
    )
    try:
        bound = wrapper.bind_tools(
            [
                {
                    "name": "lookup",
                    "description": "Look up",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ]
        )
        response = await bound.ainvoke([HumanMessage("question")])
        assert [b["type"] for b in response.content] == [b["type"] for b in blocks]
        assert response.content[0]["signature"] == "signed-fixture"
        assert response.tool_calls == [
            {"name": "lookup", "id": "local_1", "args": {"query": "fact"}, "type": "tool_call"}
        ]
        assert response.response_metadata["stop_reason"] == "tool_use"
        assert response.usage_metadata["input_tokens"] == 14
        assert response.usage_metadata["output_tokens"] == 5
        await bound.ainvoke(
            [
                HumanMessage("question"),
                response,
                ToolMessage(content="lookup result", tool_call_id="local_1"),
            ]
        )
        sent = requests[1]["messages"][1]["content"]
        assert sent[0] == blocks[0]
        assert sent[2] == blocks[2]
        assert sent[3]["content"][0]["encrypted_content"] == "opaque-result"
        assert sent[4]["id"] == "local_1"
        assert requests[0]["thinking"] == {"type": "adaptive"}
        assert requests[0]["output_config"] == {"effort": "medium"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sdk_stream_aggregates_thinking_signature_text_and_usage():
    events = [
        ("message_start", {"type": "message_start", "message": body([], None)}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "thought"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "signed"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    wire = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    wrapper, requests, client = sdk_fixture(
        lambda *_: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})
    )
    try:
        response = await wrapper.complete([HumanMessage("hi")])
        assert response.content[0]["thinking"] == "thought"
        assert response.content[0]["signature"] == "signed"
        assert response.content[1]["text"] == "hello"
        assert response.response_metadata["stop_reason"] == "end_turn"
        assert response.usage_metadata["output_tokens"] == 7
        assert requests[0]["stream"] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["ainvoke", "complete", "astream"])
@pytest.mark.parametrize(
    "message",
    [
        raw('{"ok": true}', "max_tokens"),
        raw("", "end_turn"),
        raw([{"type": "thinking", "thinking": "only", "signature": "sig"}]),
        raw("ok", None),
        raw("ok", "unknown"),
        raw("ok", "pause_turn"),
        raw([], "tool_use", tool_calls=[{"id": "bad", "name": "adb_shell", "args": {}}]),
    ],
)
async def test_final_raw_gate_rejects_before_parse_or_action(path, message):
    wrapper = gateway.RobustChatModelWrapper(RawFake(message), endpoint=endpoint())
    with pytest.raises(LLMInvalidOutputError):
        if path == "astream":
            _ = [part async for part in wrapper.astream([HumanMessage("q")])]
        else:
            await getattr(wrapper, path)([HumanMessage("q")])
    assert len(wrapper.base_model.calls) == 1


class Verdict(BaseModel):
    approved: bool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema,expected",
    [
        (Verdict, Verdict(approved=True)),
        (
            {
                "type": "object",
                "required": ["approved"],
                "properties": {"approved": {"const": True}},
            },
            {"approved": True},
        ),
        ({"type": "integer", "minimum": 3}, 5),
    ],
)
async def test_schema_first_prompt_and_contract(schema, expected):
    text = "5" if expected == 5 else '{"approved":true}'
    fake = RawFake(raw(text))
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).with_structured_output(
        schema
    )
    result = await wrapper.ainvoke(
        [SystemMessage("existing instructions"), HumanMessage("question")]
    )
    assert result == expected
    assert len([m for m in fake.calls[0] if isinstance(m, SystemMessage)]) == 1
    prompt = str(fake.calls[0][0].content)
    assert "existing instructions" in prompt and "JSON Schema" in prompt
    assert "approved" in prompt if expected != 5 else "minimum" in prompt


@pytest.mark.asyncio
async def test_schema_data_validation_one_repair_preserves_signed_blocks_and_raw():
    blocks = [
        {"type": "thinking", "thinking": "opaque", "signature": "signature"},
        {"type": "text", "text": '{"approved":"wrong"}'},
    ]
    first = raw(blocks)
    second = raw('{"approved":true}')
    fake = RawFake(first, second)
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).with_structured_output(
        Verdict, include_raw=True
    )
    result = await wrapper.ainvoke([HumanMessage("question")])
    assert result["parsed"] == Verdict(approved=True) and result["raw"] is second
    assert fake.calls[1][-2] is first
    assert fake.calls[1][-2].content == blocks


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "imaginary"},
        {"$schema": "https://unsupported.invalid/schema"},
        {"$ref": "https://remote.invalid/schema"},
        {"$ref": "#/$defs/missing"},
        {"$anchor": "present", "$ref": "#missing"},
        {"$dynamicRef": "#missing"},
        {"$schema": "https://json-schema.org/draft/2019-09/schema", "$recursiveRef": "#missing"},
        "invalid",
    ],
)
def test_invalid_or_unsupported_schema_fails_before_request(schema):
    fake = RawFake()
    with pytest.raises(Exception):
        gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).with_structured_output(schema)
    assert not fake.calls


@pytest.mark.asyncio
async def test_required_structure_fails_after_one_repair_and_truncation_never_repairs():
    fake = RawFake(raw('{"approved":"bad"}'), raw('{"approved":"bad"}'))
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).with_structured_output(
        Verdict
    )
    with pytest.raises(StructuredOutputError):
        await wrapper.ainvoke([HumanMessage("q")])
    assert len(fake.calls) == 2
    fake = RawFake(raw('{"approved":true}', "max_tokens"))
    with pytest.raises(LLMInvalidOutputError):
        await (
            gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
            .with_structured_output(Verdict)
            .ainvoke([HumanMessage("q")])
        )
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "code,category,retryable",
    [
        (1008, "balance", False),
        (1004, "authentication", False),
        (2049, "authentication", False),
        (2013, "bad_request", False),
        (2022, "permission", False),
        (1026, "content_refusal", False),
        (1027, "content_refusal", False),
        (2061, "permission", False),
        (2068, "permission", False),
        (2067, "quota_exhausted", False),
        (1001, "timeout", True),
        (1002, "rate_limit", True),
        (1024, "provider_unavailable", True),
        (1033, "provider_unavailable", True),
        (2064, "provider_unavailable", True),
        (2066, "provider_unavailable", True),
        (9999, "unknown", True),
    ],
)
def test_business_errors_are_typed_and_secret_safe(code, category, retryable):
    error = normalize_minimax_error(
        {
            "base_resp": {"status_code": code, "status_msg": "SECRET quota body"},
            "trace_id": "trace-123",
        },
        status_code=200,
    )
    assert classify_failure(error).category.value == category
    assert classify_failure(error).retryable is retryable
    assert error.trace_id == "trace-123" and "SECRET" not in str(error)
    with pytest.raises(type(error)):
        raise_for_minimax_error({"base_resp": {"status_code": code}})


@pytest.mark.asyncio
async def test_same_target_fallback_does_not_restart_retry_round(monkeypatch):
    monkeypatch.setattr(gateway.asyncio, "sleep", AsyncMock())
    wrapper, requests, client = sdk_fixture(
        lambda *_: httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "SECRET"}},
            headers={"retry-after": "0"},
        )
    )
    fallback = gateway.RobustChatModelWrapper(
        wrapper.base_model, endpoint=endpoint(timeout_seconds=12, reasoning_effort="high")
    )
    try:
        with pytest.raises(LLMExhaustedError):
            await gateway.with_fallback(
                lambda: wrapper.ainvoke([HumanMessage("q")]),
                lambda: fallback.ainvoke([HumanMessage("q")]),
            )
        assert len(requests) == 4
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_shared_budget_limits_format_repair_and_cancellation(monkeypatch):
    fake = RawFake(raw("not json"), raw('{"approved": true}'))
    with logical_call_scope(max_attempts=1):
        with pytest.raises(LLMExhaustedError):
            await (
                gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
                .with_structured_output(Verdict)
                .ainvoke([HumanMessage("q")])
            )
    assert len(fake.calls) == 1
    fake = RawFake(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).ainvoke([HumanMessage("q")])
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_native_business_permanent_error_is_not_retried():
    fake = RawFake(
        AIMessage(content="ignored", response_metadata={"base_resp": {"status_code": 1008}})
    )
    with pytest.raises(LLMPermanentError) as caught:
        await gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).ainvoke([HumanMessage("q")])
    assert caught.value.failure.category is FailureCategory.BALANCE
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_search_exact_pause_continuation_sources_and_no_device_history(monkeypatch):
    pause = raw(
        [
            {"type": "thinking", "thinking": "opaque", "signature": "signed"},
            {
                "type": "server_tool_use",
                "id": "server_7",
                "name": "web_search",
                "input": {"query": "public"},
            },
        ],
        "pause_turn",
    )
    complete = raw(
        [
            {
                "type": "web_search_tool_result",
                "tool_use_id": "server_7",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://actual.invalid/a",
                        "title": "Actual title",
                        "encrypted_content": "opaque-result",
                    }
                ],
            },
            {"type": "text", "text": "Found an answer"},
        ]
    )
    fake = RawFake(pause, complete)
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: fake)
    result = await search_service.web_search(
        SimpleNamespace(data_engine=None, device="PRIVATE HISTORY"), "public query"
    )
    assert result.status == "complete" and result.answer == "Found an answer"
    assert result.sources[0].url == "https://actual.invalid/a"
    assert result.sources[0].title == "Actual title"
    assert fake.tools == [{"type": "web_search_20250305", "name": "web_search"}]
    assert fake.calls[0] == [HumanMessage("public query")]
    assert fake.calls[1] == [HumanMessage("public query"), pause]
    assert fake.calls[1][1] is pause
    assert "PRIVATE" not in str(fake.calls)


@pytest.mark.asyncio
async def test_search_pause_exhaustion_returns_incomplete(monkeypatch):
    pause = raw(
        [{"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {}}], "pause_turn"
    )
    fake = RawFake(pause, pause, pause)
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: fake)
    result = await search_service.web_search(SimpleNamespace(data_engine=None), "query")
    assert result.status == "incomplete" and result.sources == []
    assert len(fake.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("role_enabled,global_enabled", [(True, False), (False, True)])
async def test_search_role_policy_controls_tool_and_default_controls_transport(
    monkeypatch, role_enabled, global_enabled
):
    from artemis.tools.web_search import get_web_search_tools

    default = endpoint().model_copy(
        update={"enable_grounding": global_enabled, "model_name": "DEFAULT-WIRE"}
    )
    role_ep = endpoint().model_copy(
        update={"enable_grounding": role_enabled, "model_name": "ROLE-MUST-NOT-BE-SENT"}
    )
    monkeypatch.setattr(
        gateway,
        "get_effective_endpoint",
        lambda ctx, role: default if role == "default" else role_ep,
    )
    response = raw(
        [
            {"type": "web_search_tool_result", "tool_use_id": "srv", "content": []},
            {"type": "text", "text": "No sources found"},
        ]
    )
    fake = RawFake(response)
    selected = []
    monkeypatch.setattr(ModelFactory, "get_model", lambda ep: selected.append(ep) or fake)
    ctx = SimpleNamespace(data_engine=None, llm_config=object())
    tools = get_web_search_tools(ctx, "log_analyzer")
    assert bool(tools) is role_enabled
    if tools:
        result = json.loads(await tools[0].ainvoke({"query": "public"}))
        assert result["status"] == "complete"
        assert selected == [default]
        assert "ROLE-MUST-NOT-BE-SENT" not in str(fake.calls)
    else:
        result = await search_service.web_search(ctx, "public", role="log_analyzer")
        assert result.status == "unavailable" and not selected


@pytest.mark.asyncio
async def test_search_server_permission_error_and_no_fabricated_source(monkeypatch):
    response = raw(
        [
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srv",
                "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"},
            }
        ]
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: RawFake(response))
    result = await search_service.web_search(SimpleNamespace(data_engine=None), "q")
    assert result.status == "unavailable" and not result.sources


@pytest.mark.asyncio
async def test_sse_business_error_is_permanent_and_secret_safe():
    wire = (
        "event: error\ndata: "
        + json.dumps(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "code": 1008, "message": "SECRET"},
                "trace_id": "trace-sse",
            }
        )
        + "\n\n"
    )
    wrapper, requests, client = sdk_fixture(
        lambda *_: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})
    )
    try:
        with pytest.raises(LLMPermanentError) as caught:
            await wrapper.complete([HumanMessage("q")])
        assert caught.value.failure.category is FailureCategory.BALANCE
        assert "SECRET" not in str(caught.value)
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_paid_invalid_output_still_records_usage(monkeypatch):
    seen = []
    monkeypatch.setattr(
        gateway, "record_llm_usage", lambda engine, response, **kwargs: seen.append(response)
    )
    response = raw(
        '{"approved":true}',
        "max_tokens",
        usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    )
    with pytest.raises(LLMInvalidOutputError):
        await gateway.RobustChatModelWrapper(RawFake(response), endpoint=endpoint()).ainvoke(
            [HumanMessage("q")]
        )
    assert seen == [response]


@pytest.mark.asyncio
async def test_minimax_outputter_does_not_pass_unvalidated_raw_answer():
    from artemis.config import OutputConfig

    module = importlib.import_module("artemis.agents.outputter.outputter")
    fake = RawFake(raw('{"approved":false}', "max_tokens"))
    llm = gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
    with pytest.raises(LLMInvalidOutputError):
        await module._format_structured(
            OutputConfig(structured_output=Verdict),
            SimpleNamespace(initial_goal="goal"),
            '{"approved":true}',
            None,
            llm,
            llm,
        )
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_minimax_planner_structure_error_cannot_approve(monkeypatch):
    module = importlib.import_module("artemis.agents.planner.planner")
    fake = RawFake(raw("{}"), raw("{}"))
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
    monkeypatch.setattr(module, "get_llm", lambda **kwargs: wrapper)
    ctx = SimpleNamespace(
        data_engine=None,
        execution_setup=None,
        _llm_endpoint_snapshot={("default", False, False): endpoint()},
    )
    result = await module.run_async_planner_validation(ctx, "goal", "before", "after")
    assert result["status"] == "failed"
    assert "Validation failed" in result["feedback"]


@pytest.mark.asyncio
async def test_minimax_checker_empty_report_is_error():
    from artemis.agents.checker.checker import _structured_report

    llm = gateway.RobustChatModelWrapper(RawFake(raw('{"verdicts":[]}')), endpoint=endpoint())
    with pytest.raises(LLMInvalidOutputError, match="empty verdict"):
        await _structured_report(llm, [HumanMessage("report")])


def test_explicit_empty_tools_are_preserved_and_native_server_tool_is_not_client_tool():
    fake = RawFake()
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
    bound = wrapper.bind_tools([])
    assert fake.tools == [] and not bound.client_tools
    bound = wrapper.bind_tools([search_service.SERVER_SEARCH])
    assert not bound.client_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_json", ['{"query":"fact"}', '{"query":"fact"'])
async def test_sdk_stream_client_tool_id_and_input_survive_aggregation(partial_json):
    events = [
        ("message_start", {"type": "message_start", "message": body([], None)}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool_stream",
                    "name": "lookup",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    for _, event in events[1:-2]:
        if "index" in event:
            event["index"] = 1
    events[1:1] = [
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "opaque thought"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ]
    wire = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    wrapper, requests, client = sdk_fixture(
        lambda _, count: (
            httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})
            if count == 1
            else body([{"type": "text", "text": "done"}])
        )
    )
    try:
        wrapper = wrapper.bind_tools(
            [
                {
                    "name": "lookup",
                    "description": "Look up",
                    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ]
        )
        if not partial_json.endswith("}"):
            with pytest.raises(LLMInvalidOutputError, match="incomplete streamed"):
                await wrapper.complete([HumanMessage("q")])
            assert len(requests) == 1
            return
        response = await wrapper.complete([HumanMessage("q")])
        assert response.tool_calls[0]["id"] == "tool_stream"
        assert response.tool_calls[0]["args"] == {"query": "fact"}
        await wrapper.ainvoke(
            [HumanMessage("q"), response, ToolMessage(content="result", tool_call_id="tool_stream")]
        )
        assistant = requests[1]["messages"][1]["content"]
        assert assistant[0] == {
            "type": "thinking",
            "thinking": "opaque thought",
            "signature": "opaque-signature",
        }
        assert assistant[1] == {
            "type": "tool_use",
            "id": "tool_stream",
            "name": "lookup",
            "input": {"query": "fact"},
        }
        assert requests[1]["messages"][2]["content"][0]["tool_use_id"] == "tool_stream"
        assert "partial_json" not in str(assistant) and "index" not in assistant[1]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_after_and_network_attempts_share_search_budget(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(gateway.asyncio, "sleep", sleep)
    wrapper, requests, client = sdk_fixture(
        lambda _, count: (
            httpx.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}},
                headers={"retry-after": "0.25"},
            )
            if count == 1
            else body([{"type": "text", "text": "answer"}])
        )
    )
    try:
        with logical_call_scope(max_attempts=2):
            response = await wrapper.ainvoke([HumanMessage("q")])
            assert response.content == "answer"
            with pytest.raises(LLMExhaustedError):
                await wrapper.ainvoke([HumanMessage("continuation")])
        assert len(requests) == 2
        sleep.assert_awaited_once_with(0.25)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_same_wire_format_failure_cannot_start_a_fallback_repair_round():
    fake = RawFake(raw("{}"), raw("{}"), raw('{"approved":true}'))
    primary = gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).with_structured_output(
        Verdict
    )
    fallback = gateway.RobustChatModelWrapper(
        fake, endpoint=endpoint(timeout_seconds=10)
    ).with_structured_output(Verdict)
    with pytest.raises(LLMInvalidOutputError):
        await gateway.with_fallback(
            lambda: primary.ainvoke([HumanMessage("q")]),
            lambda: fallback.ainvoke([HumanMessage("q")]),
        )
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_logical_time_budget_terminates_without_pause_or_resume():
    class SlowFake:
        calls = 0

        async def ainvoke(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(0.1)
            return raw("late")

    fake = SlowFake()
    with logical_call_scope(timeout_seconds=0.001):
        with pytest.raises(LLMExhaustedError):
            await gateway.RobustChatModelWrapper(fake, endpoint=endpoint()).ainvoke(
                [HumanMessage("q")]
            )
    assert fake.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "partial_json", ['{"query":"fact"}', '{"query":"fact"', "[]", '{"n":NaN}', '{"n":1e400}']
)
async def test_sdk_stream_server_arguments_are_complete_before_pause_continuation(partial_json):
    events = [
        ("message_start", {"type": "message_start", "message": body([], None)}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "opaque", "signature": "signed"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "server_tool_use",
                    "id": "server_stream",
                    "name": "web_search",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "pause_turn", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    wire = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    wrapper, requests, client = sdk_fixture(
        lambda _, count: (
            httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})
            if count == 1
            else body(
                [
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "server_stream",
                        "content": [],
                    },
                    {"type": "text", "text": "No matches"},
                ]
            )
        )
    )
    wrapper.allow_pause_turn = True
    wrapper = wrapper.bind_tools([search_service.SERVER_SEARCH])
    try:
        if partial_json != '{"query":"fact"}':
            with pytest.raises(LLMInvalidOutputError):
                await wrapper.complete([HumanMessage("q")])
            assert len(requests) == 1
            return
        response = await wrapper.complete([HumanMessage("q")])
        original_blocks = json.loads(json.dumps(response.content))
        assert not response.tool_calls
        assert (
            response.content[1]["input"] == {}
            and response.content[1]["partial_json"] == partial_json
        )
        await wrapper.ainvoke([HumanMessage("q"), response])
        assert requests[1]["messages"][1]["content"] == [
            {"type": "thinking", "thinking": "opaque", "signature": "signed"},
            {
                "type": "server_tool_use",
                "id": "server_stream",
                "name": "web_search",
                "input": {"query": "fact"},
            },
        ]
        assert response.content == original_blocks
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_search_rejects_truncated_nonstream_server_arguments_before_continuation(
    monkeypatch,
):
    wrapper, requests, client = sdk_fixture(
        lambda *_: body(
            [
                {
                    "type": "server_tool_use",
                    "id": "srv",
                    "name": "web_search",
                    "input": {},
                    "partial_json": '{"query":"fact"',
                }
            ],
            "pause_turn",
        )
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: wrapper.base_model)
    try:
        result = await search_service.web_search(SimpleNamespace(data_engine=None), "public")
        assert result.status == "error" and "incomplete streamed" in result.error
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_search_empty_result_cannot_collect_urls_from_inputs_or_unknown_blocks(
    monkeypatch,
):
    wrapper, requests, client = sdk_fixture(
        lambda *_: body(
            [
                {
                    "type": "server_tool_use",
                    "id": "srv",
                    "name": "web_search",
                    "input": {
                        "query": "public",
                        "url": "https://not-a-search-result.invalid/input",
                        "title": "invented from request",
                    },
                },
                {"type": "web_search_tool_result", "tool_use_id": "srv", "content": []},
                {
                    "type": "text",
                    "text": "No matches",
                    "url": "https://not-a-search-result.invalid/text",
                },
            ]
        )
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: wrapper.base_model)
    try:
        result = await search_service.web_search(SimpleNamespace(data_engine=None), "public")
        assert result.status == "complete" and result.sources == []
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_search_collects_only_result_and_official_citation_sources(monkeypatch):
    wrapper, _, client = sdk_fixture(
        lambda *_: body(
            [
                {
                    "type": "server_tool_use",
                    "id": "srv",
                    "name": "web_search",
                    "input": {
                        "query": "public",
                        "url": "https://input.invalid/",
                        "title": "not a source",
                    },
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srv",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://result.invalid/page",
                            "title": "Actual result",
                            "encrypted_content": "opaque",
                        }
                    ],
                },
                {
                    "type": "text",
                    "text": "Answer",
                    "citations": [
                        {
                            "type": "web_search_result_location",
                            "url": "https://citation.invalid/page",
                            "title": "Actual citation",
                            "cited_text": "Evidence",
                            "encrypted_index": "opaque-index",
                        },
                        {
                            "type": "char_location",
                            "url": "https://not-a-web-citation.invalid/",
                            "title": "not a web citation",
                            "cited_text": "Other",
                            "document_index": 0,
                            "document_title": "doc",
                            "start_char_index": 0,
                            "end_char_index": 5,
                        },
                    ],
                },
            ]
        )
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: wrapper.base_model)
    try:
        result = await search_service.web_search(SimpleNamespace(data_engine=None), "public")
        assert result.status == "complete"
        assert [(source.url, source.title) for source in result.sources] == [
            ("https://result.invalid/page", "Actual result"),
            ("https://citation.invalid/page", "Actual citation"),
        ]
    finally:
        await client.aclose()


@pytest.mark.parametrize("input_value", [[], None, {"n": float("inf")}, {"n": float("nan")}])
def test_standard_server_arguments_must_be_finite_json_objects(input_value):
    from artemis.llm.minimax import validate_minimax_response

    message = raw(
        [{"type": "server_tool_use", "id": "srv", "name": "web_search", "input": input_value}],
        "pause_turn",
    )
    with pytest.raises(LLMInvalidOutputError):
        validate_minimax_response(message, allow_pause_turn=True)


def test_unknown_signed_blocks_are_retained_without_tool_argument_validation():
    from artemis.llm.minimax import validate_minimax_response

    blocks = [
        {
            "type": "future_signed_block",
            "signature": "opaque",
            "input": [],
            "partial_json": "not JSON",
        },
        {"type": "text", "text": "answer"},
    ]
    message = raw(blocks)
    assert validate_minimax_response(message) is message
    assert message.content == blocks


def test_source_extraction_ignores_unknown_results_and_nested_input_citations():
    content = [
        {
            "type": "server_tool_use",
            "input": {
                "type": "text",
                "citations": [
                    {"type": "web_search_result_location", "url": "https://input.invalid/nested"}
                ],
            },
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srv",
            "content": [
                {
                    "type": "future_unknown_result",
                    "url": "https://unknown.invalid/",
                    "title": "not recognized",
                }
            ],
        },
        {"type": "future_unknown_block", "url": "https://unknown.invalid/top-level"},
    ]
    sources, error = search_service._extract_sources(content)
    assert sources == []
    assert "invalid search result entry" in error


@pytest.mark.asyncio
@pytest.mark.parametrize("search_status", ["complete", "incomplete", "unavailable", "error"])
async def test_search_tool_status_survives_real_diagnoser_consumer(monkeypatch, search_status):
    from artemis.agents.diagnoser.diagnoser import Diagnoser
    from artemis.core.tool_failure import is_tool_failure
    from artemis.tools.web_search import get_web_search_tools

    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    partial = search_service.SearchResult(
        status=search_status,
        answer="partial or complete evidence",
        sources=[
            search_service.SearchSource(url="https://actual.invalid/result", title="Actual source")
        ],
        error=None if search_status == "complete" else "permission denied or unfinished",
    )
    monkeypatch.setattr(search_service, "web_search", AsyncMock(return_value=partial))
    ctx = SimpleNamespace(data_engine=None, llm_config=object(), device=None)
    tools = get_web_search_tools(ctx, "diagnoser")
    # Exercise the actual LangChain declaration and existing consumer, rather
    # than asserting only the helper's JSON status field.
    tool_result = await tools[0].ainvoke({"query": "public"})
    assert is_tool_failure(tool_result) is (search_status != "complete")
    diagnoser = Diagnoser(ctx)
    diagnoser._llm = gateway.RobustChatModelWrapper(RawFake(), endpoint=endpoint())
    messages = await diagnoser._run_tool(
        {"name": "web_search", "id": "search_local", "args": {"query": "public"}},
        tools,
        SimpleNamespace(),
    )
    assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
    assert messages[0].status == ("success" if search_status == "complete" else "error")
    payload = json.loads(messages[0].content)
    assert payload["status"] == search_status
    assert payload["answer"] == partial.answer
    assert payload["sources"] == [source.model_dump() for source in partial.sources]


@pytest.mark.asyncio
async def test_invalid_output_releases_half_open_breaker_for_next_independent_sdk_call(monkeypatch):
    from artemis.llm.reliability import Failure

    wrapper, requests, client = sdk_fixture(
        lambda _, count: body(
            [
                {
                    "type": "text",
                    "text": '{"approved":true}' if count == 1 else "next call succeeded",
                }
            ],
            "max_tokens" if count == 1 else "end_turn",
        )
    )
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=0.1)
    key = wrapper._endpoint_key()
    breaker.record_failure(key, Failure(FailureCategory.RATE_LIMIT, True, True), now=0)
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", breaker)
    try:
        with pytest.raises(LLMInvalidOutputError, match="max_tokens"):
            await wrapper.ainvoke([HumanMessage("first")])
        assert not breaker.snapshot().get(key, {}).get("half_open_in_flight", False)
        with logical_call_scope(timeout_seconds=0.1):
            result = await wrapper.ainvoke([HumanMessage("second independent call")])
        assert result.content == "next call succeeded"
        assert len(requests) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_half_open_sdk_trial_releases_breaker_for_next_call(monkeypatch):
    from artemis.llm.reliability import Failure

    received = asyncio.Event()

    async def respond(_, count):
        if count == 1:
            received.set()
            await asyncio.Future()
        return body([{"type": "text", "text": "next call succeeded"}])

    wrapper, requests, client = sdk_fixture(respond)
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=0.1)
    key = wrapper._endpoint_key()
    breaker.record_failure(key, Failure(FailureCategory.RATE_LIMIT, True, True), now=0)
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", breaker)
    try:
        first = asyncio.create_task(wrapper.ainvoke([HumanMessage("first")]))
        await asyncio.wait_for(received.wait(), timeout=2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not breaker.snapshot().get(key, {}).get("half_open_in_flight", False)
        with logical_call_scope(timeout_seconds=0.1):
            result = await wrapper.ainvoke([HumanMessage("second independent call")])
        assert result.content == "next call succeeded"
        assert len(requests) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
@pytest.mark.parametrize(
    "search_payload",
    [
        "broken",
        None,
        {"type": "future_result"},
        [{"type": "web_search_result", "title": "missing URL", "encrypted_content": "opaque"}],
        [
            {
                "type": "web_search_result",
                "url": "https://actual.invalid/result",
                "encrypted_content": "opaque",
            }
        ],
        [
            {
                "type": "web_search_result",
                "url": "https://actual.invalid/result",
                "title": "missing opaque content",
            }
        ],
        [
            {
                "type": "web_search_result",
                "url": "https://[invalid",
                "title": "bad URL",
                "encrypted_content": "opaque",
            }
        ],
        [{"type": "future_result", "url": "https://unknown.invalid/result"}],
    ],
)
async def test_actual_search_malformed_results_cannot_be_complete_or_successful_tools(
    monkeypatch, search_payload
):
    from artemis.core.tool_failure import is_tool_failure
    from artemis.tools.web_search import get_web_search_tools

    wrapper, requests, client = sdk_fixture(
        lambda *_: body(
            [
                {
                    "type": "server_tool_use",
                    "id": "srv",
                    "name": "web_search",
                    "input": {"query": "fact"},
                },
                {"type": "web_search_tool_result", "tool_use_id": "srv", "content": search_payload},
                {"type": "text", "text": "Unverified answer"},
            ]
        )
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: wrapper.base_model)
    ctx = SimpleNamespace(data_engine=None, llm_config=object())
    try:
        tool = get_web_search_tools(ctx, "log_analyzer")[0]
        result = await tool.ainvoke({"query": "public"})
        assert is_tool_failure(result)
        payload = json.loads(result)
        assert payload["status"] == "error"
        assert "malformed search results" in payload["error"]
        assert payload["answer"] == "Unverified answer"
        assert payload["sources"] == []
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_schema_allows_pydantic_dollar_ref_alias_and_legacy_parse():
    from artemis.llm.structured import parse_structured

    class Aliased(BaseModel):
        value: str = Field(alias="$ref")

    text = '{"$ref":"literal field value"}'
    legacy = parse_structured(text, schema=Aliased)
    assert isinstance(legacy, Aliased) and legacy.value == "literal field value"
    fake = RawFake(raw(text))
    result = (
        await gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
        .with_structured_output(Aliased)
        .ainvoke([HumanMessage("q")])
    )
    assert result == legacy
    assert len(fake.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema,payload",
    [
        (
            {"type": "object", "properties": {"$ref": {"type": "string"}}, "required": ["$ref"]},
            {"$ref": "literal value"},
        ),
        (
            {
                "const": {
                    "$ref": "https://literal.invalid/not-a-schema",
                    "$dynamicRef": "#literal",
                    "$recursiveRef": "#literal",
                }
            },
            {
                "$ref": "https://literal.invalid/not-a-schema",
                "$dynamicRef": "#literal",
                "$recursiveRef": "#literal",
            },
        ),
        (
            {
                "type": "object",
                "default": {"$ref": "https://literal.invalid/default"},
                "examples": [{"$dynamicRef": "#literal"}],
            },
            {},
        ),
        (
            {
                "$defs": {
                    "nested": {
                        "$id": "https://schema.invalid/nested",
                        "$defs": {"part": {"type": "string"}},
                        "$ref": "#/$defs/part",
                    }
                },
                "$ref": "#/$defs/nested",
            },
            "hello",
        ),
        (
            {
                "$id": "https://schema.invalid/root",
                "$defs": {"nested": {"$id": "nested", "$anchor": "value", "type": "string"}},
                "$ref": "nested#value",
            },
            "hello",
        ),
    ],
)
async def test_schema_aware_preflight_preserves_literals_and_embedded_id_reference_scopes(
    schema, payload
):
    from artemis.llm.structured import validate_schema, parse_structured

    assert validate_schema(schema) is schema
    assert parse_structured(json.dumps(payload), schema=schema) == payload
    fake = RawFake(raw(json.dumps(payload)))
    result = (
        await gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
        .with_structured_output(schema)
        .ainvoke([HumanMessage("q")])
    )
    assert result == payload and len(fake.calls) == 1


@pytest.mark.asyncio
async def test_waiting_call_deadline_does_not_release_another_live_half_open_trial(monkeypatch):
    from artemis.llm.reliability import Failure

    received, release = asyncio.Event(), asyncio.Event()

    async def respond(_, count):
        received.set()
        await release.wait()
        return body([{"type": "text", "text": "owner succeeded"}])

    wrapper, requests, client = sdk_fixture(respond)
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=0.1)
    key = wrapper._endpoint_key()
    breaker.record_failure(key, Failure(FailureCategory.RATE_LIMIT, True, True), now=0)
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", breaker)
    owner = asyncio.create_task(wrapper.ainvoke([HumanMessage("half-open owner")]))
    try:
        await asyncio.wait_for(received.wait(), timeout=2)
        before = breaker.snapshot()[key]
        assert before["half_open_in_flight"] is True
        with logical_call_scope(timeout_seconds=0.01):
            with pytest.raises(LLMExhaustedError):
                await wrapper.ainvoke([HumanMessage("waiting caller")])
        assert len(requests) == 1 and not owner.done()
        assert breaker.snapshot()[key] == before
        release.set()
        assert (await owner).content == "owner succeeded"
        assert key not in breaker.snapshot()
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
            try:
                await owner
            except asyncio.CancelledError:
                pass
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
async def test_official_minimax_search_result_body_and_page_age_survive_pause_wire(monkeypatch):
    # Official MiniMax server-tools example uses content, rather than the
    # Anthropic SDK's encrypted_content field:
    # https://platform.minimax.cn/docs/guides/server-tools
    native_result = {
        "type": "web_search_result",
        "title": "Official source",
        "url": "https://official-result.invalid/page",
        "page_age": "2 days ago",
        "content": "Actual retrieved page text",
        "future_metadata": {"opaque": "keep"},
    }
    first = [
        {"type": "thinking", "thinking": "opaque", "signature": "signed"},
        {
            "type": "server_tool_use",
            "id": "srv_official",
            "name": "web_search",
            "input": {"query": "public"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srv_official",
            "content": [native_result],
        },
    ]
    wrapper, requests, client = sdk_fixture(
        lambda _, count: (
            body(first, "pause_turn")
            if count == 1
            else body([{"type": "text", "text": "Answer supported by the retrieved page"}])
        )
    )
    monkeypatch.setattr(gateway, "get_effective_endpoint", lambda *_: endpoint())
    monkeypatch.setattr(ModelFactory, "get_model", lambda _: wrapper.base_model)
    try:
        result = await search_service.web_search(SimpleNamespace(data_engine=None), "public")
        assert result.status == "complete"
        assert [(source.url, source.title) for source in result.sources] == [
            (native_result["url"], native_result["title"])
        ]
        assert len(requests) == 2
        assistant = requests[1]["messages"][1]["content"]
        assert assistant[0] == first[0]
        assert assistant[1] == first[1]
        sent_result = assistant[2]["content"][0]
        assert sent_result["content"] == native_result["content"]
        assert sent_result["page_age"] == native_result["page_age"]
        assert sent_result["future_metadata"] == native_result["future_metadata"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [{"type": "null"}, {}])
async def test_outputter_validated_null_returns_once_without_fallback_or_raw_narrative(
    monkeypatch, schema
):
    from artemis.config import OutputConfig

    module = importlib.import_module("artemis.agents.outputter.outputter")
    primary_fake, fallback_fake = RawFake(raw("null")), RawFake()
    primary = gateway.RobustChatModelWrapper(primary_fake, endpoint=endpoint())
    fallback = gateway.RobustChatModelWrapper(fallback_fake, endpoint=endpoint())
    monkeypatch.setattr(
        module, "get_llm", lambda **kwargs: fallback if kwargs.get("use_fallback") else primary
    )
    monkeypatch.setattr(module, "_initial_messages", lambda *args: [HumanMessage("goal")])
    monkeypatch.setattr(module, "_outputter_tools", lambda ctx: [])
    monkeypatch.setattr(
        module, "_run_react_loop", AsyncMock(return_value="unformatted raw narrative")
    )
    config = OutputConfig(structured_output=schema)
    assert config.needs_structured_format() is True
    result = await module.outputter(
        SimpleNamespace(data_engine=None), config, SimpleNamespace(initial_goal="goal")
    )
    assert result is None
    assert len(primary_fake.calls) == 1 and not fallback_fake.calls


@pytest.mark.asyncio
async def test_outputter_no_schema_keeps_plain_text_and_raw_none_is_not_validated_null(monkeypatch):
    from artemis.config import OutputConfig

    module = importlib.import_module("artemis.agents.outputter.outputter")
    fake = RawFake()
    wrapper = gateway.RobustChatModelWrapper(fake, endpoint=endpoint())
    monkeypatch.setattr(module, "get_llm", lambda **kwargs: wrapper)
    monkeypatch.setattr(module, "_initial_messages", lambda *args: [HumanMessage("goal")])
    monkeypatch.setattr(module, "_outputter_tools", lambda ctx: [])
    monkeypatch.setattr(module, "_run_react_loop", AsyncMock(return_value="plain narrative"))
    config = OutputConfig()
    assert config.needs_structured_format() is False
    result = await module.outputter(
        SimpleNamespace(data_engine=None), config, SimpleNamespace(initial_goal="goal")
    )
    assert result == "plain narrative" and not fake.calls
    with pytest.raises(LLMInvalidOutputError):
        await gateway.RobustChatModelWrapper(RawFake(None), endpoint=endpoint()).ainvoke(
            [HumanMessage("q")]
        )
