"""Offline actual-SDK media boundary and native ASR wire contracts."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.video_analyzer import native_speech
from artemis.agents.video_analyzer.native_speech import transcribe_audio
from artemis.agents.video_analyzer.reliability import classify_video_failure
from artemis.llm import minimax_media
from artemis.llm.minimax import normalize_minimax_error
from artemis.llm.reliability import (
    FailureCategory,
    LLMInvalidOutputError,
    LLMPermanentError,
    logical_call_scope,
)
from artemis.llm.router import ModelEndpoint, ModelFactory
from artemis.services import llm as gateway


def endpoint(**updates):
    fields = dict(
        provider="anthropic",
        vendor="minimax",
        model_name="MiniMax-M3.1-Flash",
        api_base="https://api.minimax.cn/anthropic",
        api_key="offline-fixture-key",
    )
    fields.update(updates)
    return ModelEndpoint(**fields)


def message_body():
    return {
        "id": "msg_offline",
        "type": "message",
        "role": "assistant",
        "model": "MiniMax-M3.1-Flash",
        "content": [{"type": "text", "text": "observed"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }


@pytest.mark.asyncio
async def test_actual_sdk_native_media_history_and_exact_full_body_boundary(monkeypatch):
    requests = []

    async def handle(request):
        requests.append(request.content)
        return httpx.Response(200, json=message_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sdk = anthropic.AsyncAnthropic(
            api_key="offline-fixture-key",
            base_url="https://offline.invalid/anthropic",
            http_client=client,
            max_retries=0,
        )
        model = ModelFactory.create_model(endpoint())
        assert isinstance(model, minimax_media.MiniMaxChatAnthropic)
        model._async_client = sdk
        bound = model.bind_tools(
            [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        )
        history = [
            HumanMessage(
                content=[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(b"small-image").decode(),
                        },
                    },
                    {
                        "type": "video",
                        "source": {"type": "url", "url": "https://media.invalid/video.mp4"},
                        "fps": 0.2,
                        "detail": "low",
                        "max_long_side_pixel": 720,
                    },
                    {
                        "type": "video",
                        "source": {
                            "type": "base64",
                            "media_type": "video/mp4",
                            "data": base64.b64encode(b"small-video").decode(),
                        },
                        "fps": 5,
                    },
                ]
            ),
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "opaque", "signature": "signed"},
                    {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}},
                ],
                tool_calls=[{"id": "call_1", "name": "lookup", "args": {}}],
            ),
            ToolMessage(content="历史 evidence", tool_call_id="call_1"),
        ]
        await bound.ainvoke(history)
        sent = json.loads(requests[0])
        assert sent["model"] == "MiniMax-M3.1-Flash"
        assert sent["max_tokens"] == endpoint().max_tokens
        assert sent["thinking"] == {"type": "adaptive"}
        assert sent["output_config"] == {"effort": "medium"}
        assert sent["messages"][0]["content"][1]["fps"] == 0.2
        assert sent["messages"][0]["content"][1]["detail"] == "low"
        assert (
            sent["messages"][0]["content"][2]["source"]["data"]
            == base64.b64encode(b"small-video").decode()
        )
        assert sent["messages"][1]["content"][0]["signature"] == "signed"
        assert sent["messages"][2]["content"][0]["tool_use_id"] == "call_1"
        monkeypatch.setattr(minimax_media, "MESSAGES_LIMIT", len(requests[0]))
        await bound.ainvoke(history)
        monkeypatch.setattr(minimax_media, "MESSAGES_LIMIT", len(requests[0]) - 1)
        with pytest.raises(Exception, match="complete Messages body"):
            await bound.ainvoke(history)
        assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,limit", [("image", "IMAGE_LIMIT"), ("video", "VIDEO_LIMIT")])
async def test_actual_sdk_checks_each_inline_media_before_http(monkeypatch, kind, limit):
    monkeypatch.setattr(minimax_media, limit, 5)
    called = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(called)) as client:
        model = ModelFactory.create_model(endpoint())
        model._async_client = anthropic.AsyncAnthropic(api_key="offline", http_client=client)
        with pytest.raises(Exception, match=f"{kind} exceeds"):
            await model.ainvoke(
                [
                    HumanMessage(
                        content=[
                            {
                                "type": kind,
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg" if kind == "image" else "video/mp4",
                                    "data": base64.b64encode(b"123456").decode(),
                                },
                            }
                        ]
                    )
                ]
            )
        called.assert_not_called()


@pytest.mark.asyncio
async def test_actual_sdk_media_shaped_schema_and_tool_input_are_ordinary_json():
    literal = {"type": "image", "source": {"type": "base64", "data": "not image data"}}
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=message_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = ModelFactory.create_model(endpoint())
        model._async_client = anthropic.AsyncAnthropic(api_key="offline", http_client=client)
        bound = model.bind_tools(
            [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "input_schema": {
                        "type": "object",
                        "properties": {"literal": {"const": literal, "examples": [literal]}},
                    },
                }
            ]
        )
        await bound.ainvoke(
            [
                HumanMessage(content="question"),
                AIMessage(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "lookup",
                            "input": {"literal": literal},
                        }
                    ],
                    tool_calls=[{"name": "lookup", "id": "call_1", "args": {"literal": literal}}],
                ),
                ToolMessage(content="result", tool_call_id="call_1"),
            ]
        )
    assert requests[0]["tools"][0]["input_schema"]["properties"]["literal"]["const"] == literal
    assert requests[0]["messages"][1]["content"][0]["input"]["literal"] == literal


@pytest.mark.asyncio
async def test_asr_actual_multipart_headers_and_recording_offsets(tmp_path):
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"synthetic-not-real-audio")
    requests = []

    async def handle(request):
        requests.append(request)
        await request.aread()
        return httpx.Response(
            200,
            json={
                "text": "hello",
                "segments": [{"start": 0.25, "end": 1.5, "speaker": "S1", "text": "hello"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await transcribe_audio(
            endpoint(),
            audio,
            actual_start=12.5,
            duration=2,
            work_dir=tmp_path,
            language="zh",
            client=client,
        )
    request = requests[0]
    assert str(request.url) == "https://api.minimax.cn/v1/speech_to_text"
    assert request.headers["authorization"] == "Bearer offline-fixture-key"
    assert request.headers["language"] == "zh"
    assert "multipart/form-data; boundary=" in request.headers["content-type"]
    for field, value in [
        ("model", "asr-1.0"),
        ("response_format", "verbose_json"),
        ("stream", "false"),
        ("timestamp_level", "sentence"),
    ]:
        assert f'name="{field}"'.encode() in request.content
        assert value.encode() in request.content
    assert result.segments[0] == {
        "start": 12.75,
        "end": 14.0,
        "text": "hello",
        "request_id": 1,
        "speaker": "S1",
        "speaker_scope": "request_1",
    }
    assert result.coverage[0]["start"] == 12.5


@pytest.mark.asyncio
async def test_asr_overlap_deduplicates_only_same_time_not_repeated_speech(tmp_path, monkeypatch):
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"audio")

    async def cut(source, output, offset, duration):
        output.write_bytes(b"derived")

    monkeypatch.setattr(native_speech, "_audio_slice", cut)
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        segments = (
            [{"start": 479.0, "end": 480.0, "text": "repeat"}]
            if calls == 1
            else [
                {"start": 0.0, "end": 1.0, "text": "repeat"},
                {"start": 2.0, "end": 3.0, "text": "repeat"},
            ]
        )
        return httpx.Response(200, json={"segments": segments})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await transcribe_audio(
            endpoint(), audio, actual_start=10, duration=501, work_dir=tmp_path, client=client
        )
    assert calls == 2
    assert [(s["start"], s["end"]) for s in result.segments] == [(489.0, 490.0), (491.0, 492.0)]
    assert result.coverage[-1]["end"] == 511


@pytest.mark.asyncio
async def test_asr_empty_segments_are_no_speech_and_plain_text_is_not_aligned(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    for payload in ({"segments": []}, {"text": "unsegmented"}):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ) as client:
            if "segments" in payload:
                result = await transcribe_audio(
                    endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path, client=client
                )
                assert result.status == "no_speech"
            else:
                with pytest.raises(LLMInvalidOutputError, match="no aligned segments"):
                    await transcribe_audio(
                        endpoint(),
                        audio,
                        actual_start=0,
                        duration=2,
                        work_dir=tmp_path,
                        client=client,
                    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http_status,error_type,category",
    [
        (400, "bad_request_error", "bad_request"),
        (401, "authorized_error", "authentication"),
        (402, "insufficient_balance_error", "balance"),
        (413, "unknown", "bad_request"),
        (422, "unprocessable_entity_error", "content_refusal"),
    ],
)
async def test_native_permanent_error_is_one_attempt_and_no_text_fallback_reset(
    tmp_path, http_status, error_type, category
):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(http_status, json={"error": {"type": error_type}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with logical_call_scope(max_attempts=10):
            for model in ("MiniMax-M3.1-Flash", "MiniMax-M3"):
                with pytest.raises(LLMPermanentError) as error:
                    await transcribe_audio(
                        endpoint(model_name=model),
                        audio,
                        actual_start=0,
                        duration=2,
                        work_dir=tmp_path,
                        client=client,
                    )
                assert error.value.failure.category.value == category
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error_type", [(429, "rate_limit_error"), (500, "server_error")])
async def test_asr_transient_recovery_uses_shared_budget(tmp_path, monkeypatch, status, error_type):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(status, json={"error": {"type": error_type}})
            if calls == 1
            else httpx.Response(200, json={"segments": []})
        )

    monkeypatch.setattr(gateway.asyncio, "sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with logical_call_scope(max_attempts=2):
            result = await transcribe_audio(
                endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path, client=client
            )
    assert result.status == "no_speech" and calls == 2


@pytest.mark.asyncio
async def test_asr_http200_business_failure_multipart_overhead_and_custom_gateway(
    tmp_path, monkeypatch
):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"base_resp": {"status_code": 1008}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await transcribe_audio(
            endpoint(api_base="https://gateway.invalid/anthropic"),
            audio,
            actual_start=0,
            duration=2,
            work_dir=tmp_path,
            client=client,
        )
        assert result.status == "unsupported" and calls == []
        with pytest.raises(LLMPermanentError) as error:
            await transcribe_audio(
                endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path, client=client
            )
        assert error.value.failure.category == FailureCategory.BALANCE
        # The 5-byte file fits, but actual multipart overhead does not.
        monkeypatch.setattr(native_speech, "ASR_BODY_LIMIT", 500)

        async def cut(source, output, offset, duration):
            output.write_bytes(b"audio")

        monkeypatch.setattr(native_speech, "_audio_slice", cut)
        with pytest.raises(LLMPermanentError, match="multipart body"):
            await transcribe_audio(
                endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path, client=client
            )
        assert len(calls) == 1


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (402, "balance"),
        (422, "content_refusal"),
        (500, "provider_unavailable"),
    ],
)
def test_unknown_error_type_does_not_mask_http_classification(status, category):
    error = normalize_minimax_error({"error": {"type": "future_error"}}, status_code=status)
    assert error.failure.category.value == category
    assert classify_video_failure(error).category.value == category
    unknown_code = normalize_minimax_error(
        {"error": {"type": "future_error", "code": 99999}}, status_code=status
    )
    assert unknown_code.failure.category.value == category


@pytest.mark.asyncio
async def test_cancel_asr_closes_owned_client_and_stops_future_subclips(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    started = asyncio.Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(native_speech.httpx, "AsyncClient", lambda **kw: client)
    task = asyncio.create_task(
        transcribe_audio(endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.is_closed and calls == 1
    assert audio.read_bytes() == b"audio"


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_owned_asr_client_close(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"local audio")
    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(request):
        started.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    original_close = client.aclose

    async def close():
        closing.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(client, "aclose", close)
    monkeypatch.setattr(native_speech.httpx, "AsyncClient", lambda **kw: client)
    task = asyncio.create_task(
        transcribe_audio(endpoint(), audio, actual_start=0, duration=2, work_dir=tmp_path)
    )
    await started.wait()
    task.cancel("original cancellation")
    await closing.wait()
    task.cancel("second cancellation")
    await asyncio.sleep(0.02)
    assert not task.done() and not client.is_closed
    release.set()
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert client.is_closed
