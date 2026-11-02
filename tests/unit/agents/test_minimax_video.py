"""MiniMax video orchestration, evidence, modalities and ownership regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anthropic
import httpx
from langchain_core.messages import AIMessage, ToolMessage
import pytest

from artemis.agents.video_analyzer import native_speech, universal_engine, video_analyzer
from artemis.agents.video_analyzer.blackboard import VideoBlackboard
from artemis.agents.video_analyzer.native_speech import SpeechResult
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.config.agent import VideoAnalyzerConfig
from artemis.config import AgentGlobalConfig
from artemis.config.llm import LLMConfig, _expand_default_into_nodes
from artemis.context import ArtemisContext, DeviceContext
from artemis.core.tool_failure import ToolFailure
from artemis.llm.reliability import LLMInvalidOutputError, logical_call_scope
from artemis.llm import providers
from artemis.llm.providers import LLMProviderRegistry
from artemis.llm.router import ModelEndpoint, ModelFactory
from artemis.mcp.action_executor import McpActionExecutor
from artemis.tools.video_tool import (
    VIDEO_ANALYZER_TOOL,
    VideoAnalyzerArgs,
    video_analyzer as video_tool,
    video_analyzer_pure,
)
from artemis.utils.video import communicate_process
from third_party.mobile_use.utils.video import NoAudioTrackError, VideoRecordingResult


def analyzer(monkeypatch, *, processing="auto"):
    ep = ModelEndpoint(
        provider="anthropic",
        vendor="minimax",
        model_name="MiniMax-M3.1-Flash",
        api_base="https://api.minimax.cn/anthropic",
        api_key="offline-fixture-key",
    )
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(
            utils=SimpleNamespace(video_analyzer=SimpleNamespace(model="gemini-raw-residue"))
        ),
        agent_config=SimpleNamespace(video_analyzer=VideoAnalyzerConfig(processing=processing)),
        _llm_endpoint_snapshot={("video_analyzer", True, False): ep},
        _genai_client=object(),
        data_engine=None,
        device=SimpleNamespace(device_id="offline-device"),
        _video_blackboard=None,
        _video_circuit_breaker=None,
        execution_setup=None,
    )
    # The MiniMax branch must exit before inspecting the residual Google key.
    key = MagicMock()
    key.get_secret_value.side_effect = AssertionError("Google credentials must not be read")
    monkeypatch.setattr(video_analyzer.settings, "GOOGLE_API_KEY", key)
    monkeypatch.setattr(
        video_analyzer.genai,
        "Client",
        MagicMock(side_effect=AssertionError("Google client prohibited")),
    )
    return VideoAnalyzer(ctx)


def answer(*, sufficient=True, **overrides):
    args = dict(
        summary="A button changed",
        analysis="Observed in the supplied evidence",
        timeline_events=[],
        temporal_evidence_sufficient=sufficient,
        unknowns=[],
    )
    args.update(overrides)
    return AIMessage(
        content="",
        tool_calls=[{"name": "submit_answer", "id": "call_answer", "args": args}],
        response_metadata={"stop_reason": "tool_use"},
    )


def call(name="spawn_sub_agent", **args):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "id": "call_1", "args": args}],
        response_metadata={"stop_reason": "tool_use"},
    )


async def _typed_run_job(
    monkeypatch,
    tmp_path,
    time_description,
    work,
    *,
    asr_error=False,
    reuse=False,
    reuse_instance=False,
    confirm_source=True,
    export_hook=None,
    **modalities,
):
    """Production run/config/SDK paths with only transport/controller replaced."""
    from artemis.services.llm import get_effective_endpoint
    from artemis.utils.video import get_ffmpeg_path

    registry = LLMProviderRegistry(tmp_path / "providers.json")
    registry.add(
        name="media-offline",
        kind="anthropic",
        vendor="minimax",
        api_base="https://api.minimax.cn/anthropic",
        api_key="offline-fixture-key",
        model="MiniMax-M3.1-Flash",
    )
    monkeypatch.setattr(providers, "provider_registry", registry)
    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "media-offline",
                    "model": "default",
                    "fallback": {"provider": "media-offline", "model": "default"},
                }
            }
        )
    )
    ctx = ArtemisContext(
        device=DeviceContext(device_id="offline-no-device"),
        llm_config=config,
        agent_config=AgentGlobalConfig(video_analyzer=VideoAnalyzerConfig(processing="static")),
    )
    ctx._genai_client = object()
    if confirm_source and video_analyzer.get_active_session(ctx.device.device_id) is None:
        from artemis.agents.video_analyzer import blackboard

        session = SimpleNamespace(video_id=uuid4(), generation=0)
        monkeypatch.setattr(video_analyzer, "get_active_session", lambda device: session)
        monkeypatch.setattr(blackboard, "get_active_session", lambda device: session)
    monkeypatch.setattr(
        video_analyzer.genai, "Client", MagicMock(side_effect=AssertionError("Google prohibited"))
    )
    scratch = tmp_path / "owned-scratch"
    monkeypatch.setattr(video_analyzer, "get_temp_dir", lambda name: scratch)
    source = tmp_path / "recording.mp4"
    ffmpeg = get_ffmpeg_path()
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=2:d=60",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=60",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await communicate_process(process)
    assert process.returncode == 0
    extracts = []

    class FakeController:
        async def extract_segment_metadata(self, start, end=None, output_path=None):
            end = 60.0 if end is None else end
            extracts.append((start, end))
            output = tmp_path / f"borrowed-{start}-{end}.mp4"
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-y",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(end - start),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await communicate_process(process)
            assert process.returncode == 0
            if export_hook is not None:
                export_hook()
            return VideoRecordingResult(
                success=True,
                message="offline extraction",
                video_path=output,
                duration_seconds=end - start,
                actual_start_relative_time=start,
            )

    monkeypatch.setattr(video_analyzer, "get_controller", lambda context: FakeController())
    requests = []
    coordinator_turn = 0

    def response(content, reason="end_turn"):
        return {
            "id": "msg_offline",
            "type": "message",
            "role": "assistant",
            "model": "MiniMax-M3.1-Flash",
            "content": content,
            "stop_reason": reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    async def handle(request):
        nonlocal coordinator_turn
        await request.aread()
        if request.url.path.endswith("speech_to_text"):
            requests.append(("asr", None))
            return httpx.Response(
                200, json={"base_resp": {"status_code": 1008}} if asr_error else {"segments": []}
            )
        payload = json.loads(request.content)
        if payload["tools"][0]["name"] == "submit_answer":
            requests.append(("visual", payload))
            return httpx.Response(
                200,
                json=response(
                    [
                        {
                            "type": "tool_use",
                            "id": "answer",
                            "name": "submit_answer",
                            "input": {
                                "summary": "A blue screen",
                                "analysis": "The supplied visual evidence shows a blue screen.",
                                "timeline_events": [],
                                "temporal_evidence_sufficient": True,
                                "unknowns": [],
                            },
                        }
                    ],
                    "tool_use",
                ),
            )
        requests.append(("coordinator", payload))
        coordinator_turn += 1
        if coordinator_turn == 1:
            return httpx.Response(
                200,
                json=response(
                    [
                        {"type": "tool_use", "id": f"work_{i}", "name": name, "input": args}
                        for i, (name, args) in enumerate(work)
                    ],
                    "tool_use",
                ),
            )
        return httpx.Response(
            200, json=response([{"type": "text", "text": "Finished the requested analysis."}])
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as text_client,
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as asr_client,
    ):
        endpoint = get_effective_endpoint(ctx, "video_analyzer", is_utils=True)
        raw_model = ModelFactory.create_model(endpoint)
        raw_model._async_client = anthropic.AsyncAnthropic(
            api_key="offline-fixture-key",
            base_url=endpoint.api_base,
            max_retries=0,
            http_client=text_client,
        )
        monkeypatch.setattr(ModelFactory, "get_model", classmethod(lambda cls, selected: raw_model))
        original_transcribe = native_speech.transcribe_audio

        async def transcribe(selected, audio_path, **kwargs):
            return await original_transcribe(selected, audio_path, client=asr_client, **kwargs)

        monkeypatch.setattr(native_speech, "transcribe_audio", transcribe)
        agent = VideoAnalyzer(ctx)
        outcome, status = await agent.run(
            time_description, "Inspect the requested interval", **modalities
        )
        for _ in range(int(reuse)):
            coordinator_turn = 0
            outcome, status = await (agent if reuse_instance else VideoAnalyzer(ctx)).run(
                time_description, "Inspect the requested interval", **modalities
            )
    assert source.is_file()
    assert not list(scratch.glob("media_job_*"))
    return json.loads(outcome), status, requests, extracts


@pytest.mark.asyncio
async def test_run_rejects_shorter_child_coverage_for_original_requested_interval(
    monkeypatch, tmp_path
):
    result, status, requests, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 60s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 1, "specific_query": "inspect"})],
    )
    assert status == "error" and result["status"] == "partial"
    assert result["requested_scope"]["start"] == 0 and result["requested_scope"]["end"] == 60
    assert result["missing_coverage"]["visual"] == [[1.0, 60.0]]
    assert result["modalities"]["visual"] == "partial"
    assert any(kind == "visual" for kind, _ in requests)


@pytest.mark.asyncio
async def test_run_cannot_count_successful_asr_as_required_visual_coverage(monkeypatch, tmp_path):
    result, status, requests, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 2s",
        [("analyze_audio_only", {"start_time": 0, "end_time": 2, "specific_query": "speech"})],
        speech="required",
    )
    assert status == "error" and result["status"] == "partial"
    assert result["missing_coverage"] == {"visual": [[0.0, 2.0]], "speech": []}
    assert result["modalities"]["visual"] == "unavailable"
    assert result["modalities"]["speech"][0]["status"] == "no_speech"
    assert [kind for kind, _ in requests] == ["coordinator", "asr", "coordinator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("speech_only", [False, True])
async def test_run_requires_union_without_holes_for_each_required_modality(
    monkeypatch, tmp_path, speech_only
):
    name = "analyze_audio_only" if speech_only else "spawn_sub_agent"
    result, status, _, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 2s",
        [
            (name, {"start_time": 0, "end_time": 0.5, "specific_query": "inspect"}),
            (name, {"start_time": 1, "end_time": 2, "specific_query": "inspect"}),
        ],
        visual=not speech_only,
    )
    modality = "speech" if speech_only else "visual"
    assert status == "error" and result["status"] == "partial"
    assert result["missing_coverage"][modality] == [[0.5, 1.0]]
    assert {record["modality"] for record in result["coverage"]} == {modality}


@pytest.mark.asyncio
@pytest.mark.parametrize("speech_only", [False, True])
async def test_run_overlapping_coverage_union_can_complete(monkeypatch, tmp_path, speech_only):
    name = "analyze_audio_only" if speech_only else "spawn_sub_agent"
    result, status, _, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 2s",
        [
            (name, {"start_time": start, "end_time": end, "specific_query": "inspect"})
            for start, end in [(0.0, 1.0), (0.5, 1.5), (1.0, 2.0)]
        ],
        visual=not speech_only,
    )
    modality = "speech" if speech_only else "visual"
    assert status == "success" and result["status"] == "complete"
    assert result["missing_coverage"] == {modality: []}
    if speech_only:
        assert result["modalities"]["visual"] == "not_requested"
        assert all(fact["status"] == "no_speech" for fact in result["modalities"]["speech"])


@pytest.mark.asyncio
async def test_run_optional_balance_failure_keeps_visual_complete_and_discloses_speech(
    monkeypatch, tmp_path
):
    result, status, _, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        speech="optional",
        asr_error=True,
    )
    assert status == "success" and result["status"] == "complete"
    assert result["modalities"]["visual"] == "analyzed"
    assert result["modalities"]["speech"][0]["status"] == "failed"
    assert "balance" in result["modalities"]["speech"][0]["error"]
    assert result["missing_coverage"] == {"visual": [], "speech": [[0.0, 2.0]]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "time_description,start,end",
    [("from 58s to the end", 58.0, 59.0), ("entire recording", 0.0, 1.0)],
)
async def test_run_binds_recording_end_before_work_without_shrinking_scope(
    monkeypatch, tmp_path, time_description, start, end
):
    result, status, _, extracts = await _typed_run_job(
        monkeypatch,
        tmp_path,
        time_description,
        [("spawn_sub_agent", {"start_time": start, "end_time": end, "specific_query": "inspect"})],
    )
    assert status == "error" and result["status"] == "partial"
    assert result["requested_scope"]["resolution"] == "recording_metadata"
    assert result["requested_scope"]["start"] == start and result["requested_scope"]["end"] == 60
    assert result["missing_coverage"]["visual"] == [[end, 60.0]]
    assert extracts[0] == (start, 60.0)


@pytest.mark.asyncio
async def test_run_unresolvable_user_scope_is_incomplete_without_model_or_controller_work(
    monkeypatch, tmp_path
):
    result, status, requests, extracts = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "the interesting moments",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 1, "specific_query": "inspect"})],
    )
    assert status == "error" and result["status"] == "incomplete"
    assert result["requested_scope"]["resolution"] == "unresolved"
    assert result["requested_scope"]["start"] is None and result["requested_scope"]["end"] is None
    assert requests == [] and extracts == []


@pytest.mark.asyncio
async def test_run_cached_required_modalities_retain_separate_full_coverage(monkeypatch, tmp_path):
    result, status, requests, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "from 0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        speech="required",
        reuse=True,
    )
    assert status == "success" and result["status"] == "complete"
    assert result["missing_coverage"] == {"visual": [], "speech": []}
    assert {record["modality"] for record in result["coverage"]} == {"visual", "speech"}
    assert all(record["strategy"] == "cached_verified" for record in result["coverage"])
    assert [kind for kind, _ in requests].count("visual") == 1
    assert [kind for kind, _ in requests].count("asr") == 1


@pytest.mark.asyncio
async def test_optional_asr_failure_survives_public_cache_without_another_native_call(
    monkeypatch, tmp_path
):
    result, status, requests, extracts = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        speech="optional",
        asr_error=True,
        reuse=True,
    )
    assert status == "success" and result["status"] == "complete"
    assert any(
        fact["status"] == "failed" and fact["error_category"] == "balance"
        for fact in result["modalities"]["speech"]
    )
    assert any(error["category"] == "balance" for error in result["errors"])
    assert all(row["modality"] == "visual" for row in result["coverage"])
    assert [kind for kind, _ in requests].count("asr") == 1
    assert [kind for kind, _ in requests].count("visual") == 1 and len(extracts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_instance", [False, True])
async def test_same_context_different_recording_uuid_never_reuses_other_recording(
    monkeypatch, tmp_path, reuse_instance
):
    from artemis.agents.video_analyzer import blackboard

    sessions = [SimpleNamespace(video_id=uuid4(), generation=0) for _ in range(2)]
    current = [sessions[0]]
    monkeypatch.setattr(blackboard, "get_active_session", lambda device: current[0])
    monkeypatch.setattr(video_analyzer, "get_active_session", lambda device: current[0])
    original = VideoAnalyzer.run
    boards = []

    async def run(self, *args, **kwargs):
        current[0] = sessions[min(len(boards), 1)]
        result = await original(self, *args, **kwargs)
        boards.append(self.blackboard)
        return result

    monkeypatch.setattr(VideoAnalyzer, "run", run)
    result, status, requests, extracts = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        reuse=True,
        reuse_instance=reuse_instance,
    )
    assert status == "success" and len(extracts) == 2
    assert [kind for kind, _ in requests].count("visual") == 2
    assert result["recording_source"]["video_id"] == str(sessions[1].video_id)
    assert boards[0].board_key != boards[1].board_key
    assert all(board.metrics()["segments"]["succeeded"] == 1 for board in boards)


@pytest.mark.asyncio
async def test_unconfirmed_recording_is_not_reused_across_jobs(monkeypatch, tmp_path):
    result, status, requests, extracts = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        reuse=True,
        reuse_instance=True,
        confirm_source=False,
    )
    assert status == "success" and not result["recording_source"]["confirmed"]
    assert result["recording_source"]["video_id"] is None
    assert len(extracts) == 2 and [kind for kind, _ in requests].count("visual") == 2


@pytest.mark.asyncio
async def test_recording_change_during_export_stops_before_native_asr(monkeypatch, tmp_path):
    from artemis.agents.video_analyzer import blackboard

    old, new = (SimpleNamespace(video_id=uuid4(), generation=0) for _ in range(2))
    current = [old]
    monkeypatch.setattr(blackboard, "get_active_session", lambda device: current[0])
    monkeypatch.setattr(video_analyzer, "get_active_session", lambda device: current[0])

    def change_source():
        current[0] = new

    result, status, requests, _ = await _typed_run_job(
        monkeypatch,
        tmp_path,
        "0s to 2s",
        [("spawn_sub_agent", {"start_time": 0, "end_time": 2, "specific_query": "inspect"})],
        speech="required",
        export_hook=change_source,
    )
    assert status == "error" and result["status"] != "complete"
    assert result["recording_source"]["video_id"] == str(old.video_id)
    assert not any(kind in {"asr", "visual"} for kind, _ in requests)
    assert any(error["category"] == "recording_source_changed" for error in result["errors"])


@pytest.mark.asyncio
async def test_partial_then_gap_repair_then_full_cache_keeps_successful_union(
    monkeypatch, tmp_path
):
    work = [("spawn_sub_agent", {"start_time": 0, "end_time": 1, "specific_query": "inspect"})]
    original = VideoAnalyzer.run
    statuses = []

    async def run(self, *args, **kwargs):
        if statuses:
            work[0][1]["end_time"] = 2
        result = await original(self, *args, **kwargs)
        statuses.append(result[1])
        return result

    monkeypatch.setattr(VideoAnalyzer, "run", run)
    result, status, requests, extracts = await _typed_run_job(
        monkeypatch, tmp_path, "0s to 2s", work, reuse=2
    )
    assert statuses == ["error", "success", "success"]
    assert status == "success" and result["missing_coverage"] == {"visual": []}
    assert all(record["strategy"] == "cached_verified" for record in result["coverage"])
    assert len(extracts) == 2 and [kind for kind, _ in requests].count("visual") == 2


def test_pinned_utility_endpoint_ignores_raw_gemini_key_and_shared_client(monkeypatch):
    agent = analyzer(monkeypatch)
    assert agent.is_minimax and not agent.use_native_gemini and agent.client is None
    assert agent.model_name == "MiniMax-M3.1-Flash" and agent.video_processing == "static"
    assert agent.ctx._genai_client is not None


@pytest.mark.asyncio
async def test_explicit_agentic_and_acoustic_are_unsupported_without_model_calls(monkeypatch):
    for processing, kwargs in [("agentic", {}), ("static", {"acoustic": True})]:
        agent = analyzer(monkeypatch, processing=processing)
        agent._invoke_universal_model = AsyncMock(side_effect=AssertionError("No call expected"))
        text, status = await agent.run("0s to 2s", "inspect", **kwargs)
        assert status == "error" and json.loads(text)["status"] == "incomplete"
        agent._invoke_universal_model.assert_not_awaited()


def test_video_tool_schema_retains_types_defaults_and_legacy_required_fields():
    schema = VideoAnalyzerArgs.model_json_schema()
    assert schema["required"] == ["time_description", "purpose"]
    assert schema["properties"]["visual"]["type"] == "boolean"
    assert schema["properties"]["speech"]["default"] == "not_requested"
    assert VIDEO_ANALYZER_TOOL.parameters == schema


@pytest.mark.asyncio
async def test_keyframes_cover_full_interval_tail_and_action_windows(monkeypatch, tmp_path):
    agent = analyzer(monkeypatch)
    agent._action_timestamps = lambda start, end: [50.0]
    calls = []

    def frames(path, timestamps, **kwargs):
        calls.append(timestamps)
        return [(round(t, 3), b"jpeg") for t in timestamps]

    monkeypatch.setattr(video_analyzer, "extract_frames_at_timestamps", frames)
    values = universal_engine._gather_universal_frames(agent, tmp_path / "v.mp4", 10, 70, 10)
    assert values[0][0] == 0 and values[-1][0] == 59.5
    assert len(calls) == 1 and len(calls[0]) <= 54
    assert any(38 <= timestamp <= 42 for timestamp, _ in values)
    assert len(values) <= 54


@pytest.mark.asyncio
async def test_native_inline_upgrade_uses_same_messages_with_original_timestamps(
    monkeypatch, tmp_path
):
    agent = analyzer(monkeypatch)
    raw = tmp_path / "video.mp4"
    raw.write_bytes(b"video")
    monkeypatch.setattr(video_analyzer, "probe_duration", AsyncMock(return_value=60))
    monkeypatch.setattr(
        video_analyzer,
        "extract_frames_at_timestamps",
        lambda p, times, **kw: [(t, b"jpeg") for t in times],
    )
    agent._invoke_universal_model = AsyncMock(
        side_effect=[answer(sufficient=False, unknowns=["transition between samples"]), answer()]
    )
    result = await agent._exec_single_chunk_universal(
        raw, raw, 10, 70, 10, "Original purpose", "query"
    )
    assert (
        "A button changed" in result
        and agent.media_coverage[0]["strategy"] == "native_inline_video"
    )
    first, second = agent._invoke_universal_model.await_args_list
    assert first.args[0][1].content[1]["text"].endswith("10.000s")
    blocks = second.args[0][1].content
    assert blocks[1]["type"] == "video" and blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["fps"] == 5 and "10.000s" in blocks[0]["text"]
    assert raw.read_bytes() == b"video"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": {"bad": "object"}},
        {"analysis": ""},
        {
            "timeline_events": [
                {
                    "start_time": float("nan"),
                    "end_time": 2.0,
                    "transcription": "claim",
                    "confidence_score": 0.8,
                    "verification_timestamp_secs": 1.0,
                }
            ]
        },
    ],
)
async def test_raw_and_typed_subagent_gate_never_invents_a_complete_answer(
    monkeypatch, tmp_path, overrides
):
    agent = analyzer(monkeypatch)
    raw = tmp_path / "video.mp4"
    raw.write_bytes(b"video")
    monkeypatch.setattr(video_analyzer, "probe_duration", AsyncMock(return_value=3))
    monkeypatch.setattr(
        video_analyzer, "extract_frames_at_timestamps", lambda p, times, **kw: [(0, b"jpeg")]
    )
    agent._invoke_universal_model = AsyncMock(return_value=answer(**overrides))
    with pytest.raises(LLMInvalidOutputError):
        await agent._exec_single_chunk_universal(raw, raw, 0, 3, 0, "purpose", "query")
    assert agent.media_coverage == [] and agent.blackboard_entries == []


@pytest.mark.asyncio
async def test_required_speech_and_optional_failure_have_distinct_completion(monkeypatch, tmp_path):
    raw = tmp_path / "video.mp4"
    raw.write_bytes(b"video")
    monkeypatch.setattr(video_analyzer, "probe_duration", AsyncMock(return_value=3))
    monkeypatch.setattr(
        video_analyzer,
        "extract_frames_at_timestamps",
        lambda p, times, **kw: [(0, b"jpeg"), (2.5, b"jpeg")],
    )
    for speech, asr_status, should_fail in [
        ("required", "no_track", True),
        ("required", "no_speech", False),
        ("optional", "failed", False),
        ("optional", "unsupported", False),
        ("not_requested", "no_track", False),
    ]:
        agent = analyzer(monkeypatch)
        agent.speech_requirement = speech
        transcribe = AsyncMock(
            return_value=SpeechResult(
                status=asr_status,
                error="not analyzed" if asr_status == "failed" else None,
                coverage=[{"start": 0.0, "end": 3.0, "evidence": "native_asr"}]
                if asr_status in {"no_speech", "transcribed"}
                else [],
            )
        )
        monkeypatch.setattr(universal_engine, "transcribe_video", transcribe)
        agent._invoke_universal_model = AsyncMock(return_value=answer())
        if should_fail:
            with pytest.raises(LLMInvalidOutputError, match="Required speech"):
                await agent._exec_single_chunk_universal(raw, raw, 0, 3, 0, "purpose", "query")
            agent._invoke_universal_model.assert_not_awaited()
        else:
            result = await agent._exec_single_chunk_universal(raw, raw, 0, 3, 0, "purpose", "query")
            assert (
                f'"status": "{asr_status if speech != "not_requested" else "not_requested"}"'
                in result
            )
        if speech == "not_requested":
            transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_track_and_ffprobe_failure_remain_distinct(monkeypatch, tmp_path):
    agent = analyzer(monkeypatch)
    monkeypatch.setattr(agent, "_scratch_dir", lambda: tmp_path)
    monkeypatch.setattr(
        native_speech,
        "extract_audio_from_video",
        AsyncMock(side_effect=NoAudioTrackError("no audio")),
    )
    result = await native_speech.transcribe_video(agent, tmp_path / "video.mp4", 0)
    assert result.status == "no_track"
    monkeypatch.setattr(
        native_speech,
        "extract_audio_from_video",
        AsyncMock(side_effect=RuntimeError("ffprobe corrupt")),
    )
    with pytest.raises(RuntimeError, match="ffprobe corrupt"):
        await native_speech.transcribe_video(agent, tmp_path / "video.mp4", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exhaustion", "tool_error", "thinking_only", "no_evidence"])
async def test_coordinator_never_reports_complete_for_unfinished_evidence(monkeypatch, mode):
    agent = analyzer(monkeypatch)
    if mode == "exhaustion":
        agent._invoke_universal_model = AsyncMock(
            return_value=call("extract_segment_metadata", start_time=0, end_time=3)
        )
        agent.exec_extract_segment_metadata = AsyncMock(return_value="metadata")
    elif mode == "tool_error":

        async def child(*args):
            agent.media_coverage.append({"start": 0, "end": 1, "evidence": "partial"})
            return ToolFailure("some chunks failed")

        agent.exec_spawn_sub_agent = child
        agent._invoke_universal_model = AsyncMock(
            side_effect=[
                call(start_time=0, end_time=3, specific_query="purpose"),
                AIMessage(content="done"),
            ]
        )
    elif mode == "thinking_only":
        agent._invoke_universal_model = AsyncMock(
            return_value=AIMessage(
                content=[{"type": "thinking", "thinking": "internal", "signature": "opaque"}]
            )
        )
    else:
        agent._invoke_universal_model = AsyncMock(
            return_value=AIMessage(content="Unsubstantiated claim")
        )
    text, status = await agent._run_universal("0s to 3s", "Original goal", "system")
    result = json.loads(text)
    assert status == "error" and result["status"] in {"partial", "incomplete", "failed"}
    if mode == "tool_error":
        tool_message = agent._invoke_universal_model.await_args_list[-1].args[0][-2]
        assert isinstance(tool_message, ToolMessage) and tool_message.status == "error"


@pytest.mark.asyncio
async def test_owned_cleanup_keeps_recording_borrowed_cache_siblings_and_evidence(
    monkeypatch, tmp_path
):
    agent = analyzer(monkeypatch)
    monkeypatch.delenv("ARTEMIS_DEBUG", raising=False)
    monkeypatch.delenv("KEEP_VIDEOS", raising=False)
    monkeypatch.setattr(video_analyzer, "get_temp_dir", lambda name: tmp_path / "scratch")
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"recording")
    sibling = tmp_path / "shared-cache.mp4"
    sibling.write_bytes(b"borrowed")
    evidence = tmp_path / "persistent.jpg"
    evidence.write_bytes(b"evidence")
    own = agent._own_segment(source)
    # A returned parent accidentally registered by an older caller is borrowed.
    agent.local_dirs_to_cleanup.add(tmp_path)
    await agent._cleanup_universal_resources()
    assert not own.exists() and source.read_bytes() == b"recording"
    assert sibling.read_bytes() == b"borrowed" and evidence.read_bytes() == b"evidence"


@pytest.mark.asyncio
async def test_cancel_media_process_terminates_and_awaits_before_propagation():
    started = asyncio.Event()
    proc = MagicMock(returncode=None)

    async def communicate():
        started.set()
        await asyncio.Event().wait()

    proc.communicate = AsyncMock(side_effect=communicate)

    def terminate():
        proc.returncode = -15
        proc.communicate = AsyncMock(return_value=(b"", b""))

    proc.terminate.side_effect = terminate
    task = asyncio.create_task(communicate_process(proc))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc.terminate.assert_called_once()
    proc.communicate.assert_awaited_once()


@pytest.mark.parametrize("persistent", [False, True])
def test_blackboard_identity_keeps_old_source_results_without_reusing_them(tmp_path, persistent):
    board = VideoBlackboard("recording", db_path=tmp_path / "board.sqlite" if persistent else None)
    for identity in (None, "google:old", "minimax:model1:source1:strategy1"):
        claimed = board.claim_segment(0, 3, "query", cache_identity=identity)
        board.complete_segment(
            0, 3, "query", claimed.lease_owner, f"summary {identity}", cache_identity=identity
        )
    assert len(board.list_ledger_entries()) == 3
    assert (
        board.missing_intervals(0, 3, "query", cache_identity="minimax:model1:source1:strategy1")
        == []
    )
    assert board.missing_intervals(
        0, 3, "query", cache_identity="minimax:model2:source1:strategy1"
    ) == [(0, 3)]
    assert board.missing_intervals(
        0, 3, "query", cache_identity="minimax:model1:source2:strategy1"
    ) == [(0, 3)]
    assert board.missing_intervals(
        0, 3, "query", cache_identity="minimax:model1:source1:strategy2"
    ) == [(0, 3)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "partial", "incomplete", "failed"])
async def test_all_video_consumers_preserve_typed_failure(monkeypatch, status):
    agent = analyzer(monkeypatch)
    monkeypatch.setattr(VideoAnalyzer, "run", AsyncMock(return_value=("structured facts", status)))
    for tool in (video_tool, video_analyzer_pure):
        value = await tool.execute(ctx=agent.ctx, time_description="0s to 3s", purpose="purpose")
        assert isinstance(value, ToolFailure)
    executor = object.__new__(McpActionExecutor)
    executor.ctx = agent.ctx
    value, ok = await executor._video_analyzer("0s to 3s", "purpose")
    assert not ok and "structured facts" in value


@pytest.mark.asyncio
async def test_background_diagnoser_failure_is_failed_consumable_message(monkeypatch):
    from artemis.agents.diagnoser import diagnoser

    agent = object.__new__(diagnoser.Diagnoser)
    agent.ctx = SimpleNamespace(
        background_jobs={"job": {"tool_call_id": "call_1", "status": "running"}}, data_engine=None
    )
    monkeypatch.setattr(
        diagnoser,
        "invoke_tool_with_injection",
        AsyncMock(
            return_value=ToolMessage(
                content='{"status":"partial"}', tool_call_id="call_1", status="error"
            )
        ),
    )
    await agent._run_video_job(
        "job", {"id": "call_1", "args": {}}, [SimpleNamespace(name="video_analyzer")], None
    )
    assert agent.ctx.background_jobs["job"]["status"] == "failed"
    messages = agent._drain_finished_jobs()
    assert len(messages) == 1 and messages[0].status == "error"
    assert agent._drain_finished_jobs() == []


@pytest.mark.asyncio
async def test_job_budget_spans_coordinator_and_child_calls(monkeypatch):
    from artemis.services import llm as gateway

    agent = analyzer(monkeypatch)
    wire_calls = 0

    async def invoke(*args, **kwargs):
        async def wire():
            nonlocal wire_calls
            wire_calls += 1
            return call("extract_segment_metadata", start_time=0, end_time=3)

        return await gateway._run_with_recovery(
            wire, provider="minimax", minimax_target="offline-video-budget"
        )

    agent._invoke_universal_model = invoke

    async def metadata(*args):
        async def wire():
            nonlocal wire_calls
            wire_calls += 1
            return "metadata"

        return await gateway._run_with_recovery(
            wire, provider="minimax", minimax_target="offline-child-budget"
        )

    agent.exec_extract_segment_metadata = metadata
    with logical_call_scope(max_attempts=3):
        text, status = await agent._run_universal("0s to 3s", "purpose", "system")
    assert wire_calls == 3 and status == "error"
    assert "budget exhausted" in text


@pytest.mark.asyncio
async def test_run_cancellation_cleans_owned_derivatives_and_keeps_borrowed_source(
    monkeypatch, tmp_path
):
    agent = analyzer(monkeypatch)
    monkeypatch.delenv("ARTEMIS_DEBUG", raising=False)
    monkeypatch.delenv("KEEP_VIDEOS", raising=False)
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(video_analyzer, "get_temp_dir", lambda name: scratch)
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"recording")
    sibling = tmp_path / "borrowed-cache.mp4"
    sibling.write_bytes(b"cache")
    controller = SimpleNamespace(
        extract_segment_metadata=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                video_path=source,
                actual_start_relative_time=0.0,
                duration_seconds=3.0,
                message="ok",
            )
        )
    )
    monkeypatch.setattr(video_analyzer, "get_controller", lambda ctx: controller)
    started = asyncio.Event()

    async def child(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    agent._exec_single_chunk_universal = child
    agent._invoke_universal_model = AsyncMock(
        return_value=call(start_time=0, end_time=3, specific_query="purpose")
    )
    task = asyncio.create_task(agent.run("0s to 3s", "purpose"))
    await started.wait()
    assert list(scratch.glob("media_job_*/recording.mp4"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(scratch.glob("media_job_*"))
    assert source.read_bytes() == b"recording" and sibling.read_bytes() == b"cache"
    assert agent.blackboard.metrics()["segments"].get("succeeded", 0) == 0


@pytest.mark.asyncio
async def test_actual_ffmpeg_sampling_audio_and_cancelled_writer(tmp_path, monkeypatch):
    from artemis.utils.video import extract_frames_at_timestamps, get_ffmpeg_path
    from third_party.mobile_use.utils.video import extract_audio_from_video, probe_duration

    ffmpeg = get_ffmpeg_path()
    path = tmp_path / "silent-60s.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=2:d=60",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await communicate_process(process)
    assert process.returncode == 0
    assert await probe_duration(path) == pytest.approx(60, abs=0.05)
    frames = extract_frames_at_timestamps(path, [0, 30, 59.5])
    assert [timestamp for timestamp, _ in frames] == [0.0, 30.0, 59.5]
    agent = analyzer(monkeypatch)
    agent.blackboard = VideoBlackboard(
        "original-recording",
        db_path=tmp_path / "board.sqlite",
        evidence_dir=tmp_path / "persistent-evidence",
    )
    monkeypatch.setattr(video_analyzer, "get_temp_dir", lambda name: tmp_path / "run-scratch")
    await universal_engine._persist_universal_events(
        agent,
        [
            {
                "start_time": 12.0,
                "end_time": 13.0,
                "verification_timestamp_secs": 12.2,
                "transcription": "blue frame",
                "confidence_score": 0.8,
            }
        ],
        10,
        70,
        10,
        path,
        "inspect",
        "blue frame",
    )
    observation = agent.blackboard.list_observations()[0]
    assert observation["requested_verification_timestamp_secs"] == 12.2
    assert observation["verification_timestamp_secs"] == 12.5
    assert observation["observed_frame_interval"] == [12.5, 12.5]
    evidence_path = Path(observation["screenshot"])
    assert evidence_path.parent == tmp_path / "persistent-evidence"
    await agent._cleanup_universal_resources()
    assert evidence_path.is_file() and path.is_file()
    with pytest.raises(NoAudioTrackError):
        await extract_audio_from_video(path, output_path=tmp_path / "speech.mp3")
    writing = tmp_path / "writing.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=64x64:r=5:d=60",
        "-c:v",
        "libx264",
        str(writing),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    task = asyncio.create_task(communicate_process(process))
    for _ in range(40):
        if writing.exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    size = writing.stat().st_size if writing.exists() else 0
    await asyncio.sleep(0.1)
    assert (writing.stat().st_size if writing.exists() else 0) == size


@pytest.mark.asyncio
async def test_slow_decoder_keeps_heartbeat_and_releases_before_cancel_finishes(
    monkeypatch, tmp_path
):
    import threading
    import time
    import cv2
    import numpy as np
    from artemis.utils import video

    agent = analyzer(monkeypatch)
    released = threading.Event()

    class SlowCapture:
        index = 0

        def isOpened(self):
            return True

        def read(self):
            time.sleep(0.01)
            self.index += 1
            return True, np.zeros((4, 4, 3), dtype=np.uint8)

        def get(self, prop):
            if prop == cv2.CAP_PROP_POS_MSEC:
                return self.index * 10
            return 100.0

        def release(self):
            released.set()

    capture = SlowCapture()
    monkeypatch.setattr(video.cv2, "VideoCapture", lambda path: capture)
    task = asyncio.create_task(
        universal_engine._gather_minimax_frames(agent, tmp_path / "slow.mp4", 0, 60, 0)
    )
    heartbeats = 0
    for _ in range(5):
        await asyncio.sleep(0.005)
        heartbeats += 1
    assert not task.done() and heartbeats == 5
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()

    final_reads = capture.index
    await asyncio.sleep(0.03)
    assert capture.index == final_reads
    released.clear()
    capture.index = 0
    with logical_call_scope(timeout_seconds=0.025):
        with pytest.raises(TimeoutError, match="frame extraction deadline"):
            await universal_engine._gather_minimax_frames(agent, tmp_path / "slow.mp4", 0, 60, 0)
    assert released.is_set()


@pytest.mark.asyncio
async def test_repeated_run_cancel_waits_for_decoder_before_deleting_owned_media(
    monkeypatch, tmp_path
):
    import threading

    agent = analyzer(monkeypatch)
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(video_analyzer, "get_temp_dir", lambda name: scratch)
    source = tmp_path / "borrowed.mp4"
    source.write_bytes(b"borrowed")
    controller = SimpleNamespace(
        extract_segment_metadata=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                video_path=source,
                actual_start_relative_time=0.0,
                duration_seconds=3.0,
                message="ok",
            )
        )
    )
    monkeypatch.setattr(video_analyzer, "get_controller", lambda ctx: controller)
    monkeypatch.setattr(video_analyzer, "probe_duration", AsyncMock(return_value=3.0))
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    decoded_paths = []

    def decode(path, *args, **kwargs):
        decoded_paths.append(path)
        started.set()
        try:
            release.wait(5.0)
            return []
        finally:
            done.set()

    monkeypatch.setattr(video_analyzer, "extract_frames_at_timestamps", decode)
    agent._invoke_universal_model = AsyncMock(
        return_value=call(start_time=0, end_time=3, specific_query="inspect")
    )
    task = asyncio.create_task(agent.run("0s to 3s", "inspect"))
    try:
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        task.cancel("original cancellation")
        await asyncio.sleep(0.02)
        task.cancel("second cancellation")
        await asyncio.sleep(0.02)
        assert not task.done() and not done.is_set() and decoded_paths[0].is_file()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        assert done.is_set() and not decoded_paths[0].exists() and source.is_file()
    finally:
        release.set()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_repeated_process_cancel_waits_for_actual_reap():
    class Process:
        returncode = None
        calls = 0

        def __init__(self):
            self.started, self.release, self.reaped = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )

        async def communicate(self):
            self.calls += 1
            self.started.set()
            if self.calls == 1:
                await asyncio.Event().wait()
            await self.release.wait()
            self.returncode = -15
            return b"", b""

        def terminate(self):
            pass

        def kill(self):
            self.returncode = -9

        async def wait(self):
            await self.release.wait()
            self.reaped.set()
            return self.returncode

    process = Process()
    task = asyncio.create_task(communicate_process(process))
    await process.started.wait()
    task.cancel("original cancellation")
    await asyncio.sleep(0.02)
    task.cancel("second cancellation")
    await asyncio.sleep(0.02)
    assert not task.done() and not process.reaped.is_set()
    process.release.set()
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert process.reaped.is_set()


def test_recording_board_switch_keeps_prior_persistent_facts(monkeypatch, tmp_path):
    from artemis.agents.video_analyzer import blackboard

    session = SimpleNamespace(video_id=uuid4(), generation=0)
    monkeypatch.setattr(blackboard, "get_active_session", lambda device: session)
    ctx = SimpleNamespace(
        device=SimpleNamespace(device_id="local"),
        data_engine=None,
        execution_setup=SimpleNamespace(traces_path=tmp_path, trace_name="trace"),
    )
    old = blackboard.get_video_blackboard(ctx)
    claim = old.claim_segment(0, 2, "inspect")
    facts = {
        "speech": [{"status": "failed", "error_category": "balance", "error": "balance"}],
        "errors": [{"category": "balance"}],
    }
    old.complete_segment(0, 2, "inspect", claim.lease_owner, "visual", media_facts=facts)
    session.video_id = uuid4()
    current = blackboard.get_video_blackboard(ctx)
    assert current.board_key != old.board_key and current.db_path == old.db_path
    assert current.missing_intervals(0, 2, "inspect") == [(0.0, 2.0)]
    restored = VideoBlackboard(old.board_key, db_path=old.db_path)
    assert restored.claim_segment(0, 2, "inspect").media_facts == facts
    assert restored.successful_segments(0, 2, "inspect")[0]["media_facts"] == facts
