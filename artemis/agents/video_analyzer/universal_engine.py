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

"""Universal (OpenAI-compatible / LangChain) execution paths for the video analyzer.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators (``extract_keyframes_from_video``, ``extract_audio_from_video``,
semaphores, retry helper, ...) are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.
"""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import glob
import json
import math
from pathlib import Path
import re
from threading import Event
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.reliability import (
    MediaChunkResult,
    SubAgentAnswerExhausted,
    classify_video_failure,
)
from artemis.agents.video_analyzer.blackboard import uncovered_intervals
from artemis.agents.video_analyzer.chunk_conversation import _validate_answer
from artemis.agents.video_analyzer.native_speech import SpeechResult, transcribe_video
from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.llm.reliability import LLMInvalidOutputError, classify_failure, logical_call_scope
from artemis.llm.structured import content_to_text
from artemis.utils.video import communicate_process, drain_media_task, get_ffmpeg_path
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class _TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_time: float = Field(strict=True, allow_inf_nan=False)
    end_time: float = Field(strict=True, allow_inf_nan=False)
    transcription: StrictStr
    confidence_score: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    verification_timestamp_secs: float = Field(strict=True, allow_inf_nan=False)


class _MiniMaxAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: StrictStr = Field(min_length=1)
    analysis: StrictStr = Field(min_length=1)
    timeline_events: list[_TimelineEvent] = Field(default_factory=list)
    temporal_evidence_sufficient: StrictBool
    unknowns: list[StrictStr]


def _minimax_submit_tool() -> dict:
    tool = deepcopy(_va.UNIVERSAL_SUBMIT_ANSWER_TOOL)
    tool["function"]["parameters"] = _MiniMaxAnswer.model_json_schema()
    return tool


def _minimax_answer(response, start: float, end: float) -> _MiniMaxAnswer:
    from artemis.llm.minimax import validate_minimax_response

    validate_minimax_response(response, client_tools=frozenset({"submit_answer"}))
    calls = getattr(response, "tool_calls", None) or []
    if len(calls) != 1 or calls[0].get("name") != "submit_answer":
        raise LLMInvalidOutputError("MiniMax video analysis requires one typed submit_answer")
    try:
        answer = _MiniMaxAnswer.model_validate(calls[0].get("args"))
    except ValueError as exc:
        raise LLMInvalidOutputError(
            "MiniMax video answer does not satisfy the media schema"
        ) from exc
    events = [event.model_dump() for event in answer.timeline_events]
    invalid = _validate_answer(answer.summary.strip(), events, start, end)
    if (
        not answer.analysis.strip()
        or invalid
        or any(
            event.start_time < start or event.end_time > end or event.start_time > event.end_time
            for event in answer.timeline_events
        )
    ):
        raise LLMInvalidOutputError(invalid or "MiniMax video event interval is invalid")
    return answer


async def _speech_for_chunk(
    analyzer, path: Path, actual_start: float, *, required=False, observed_end: float | None = None
) -> SpeechResult:
    analyzer._assert_recording_source()
    try:
        result = await transcribe_video(analyzer, path, actual_start)
    except Exception as exc:
        failure = classify_failure(exc)
        analyzer.media_errors.append(
            {
                "modality": "speech",
                "category": failure.category.value,
                "error": str(exc),
                "required": required,
            }
        )
        result = SpeechResult(
            status="failed",
            error=f"{failure.category.value}: {exc}",
            error_category=failure.category.value,
        )
        if required:
            analyzer.speech_facts.append(result.model_dump())
            raise
    analyzer.speech_facts.append(result.model_dump())
    if required and result.status not in {"transcribed", "no_speech"}:
        raise LLMInvalidOutputError(
            f"Required speech unavailable: {result.status}; {result.error or ''}"
        )
    if result.status in {"transcribed", "no_speech"}:
        spans = []
        for record in result.coverage:
            left, right = record.get("start"), record.get("end")
            if (
                isinstance(left, (int, float))
                and not isinstance(left, bool)
                and math.isfinite(left)
                and isinstance(right, (int, float))
                and not isinstance(right, bool)
                and math.isfinite(right)
            ):
                left = max(actual_start, left)
                right = min(observed_end, right) if observed_end is not None else right
                if right > left:
                    spans.append((left, right))
                    analyzer.media_coverage.append(
                        {
                            **record,
                            "modality": "speech",
                            "start": left,
                            "end": right,
                            "strategy": "native_asr",
                        }
                    )
        if (
            required
            and observed_end is not None
            and uncovered_intervals(actual_start, observed_end, spans)
        ):
            raise LLMInvalidOutputError("Required speech coverage is incomplete")
    return result


async def _inline_video(analyzer, raw_path: Path, duration: float) -> Path:
    """Use a bounded original-speed clip; inspect any derived output."""
    if raw_path.stat().st_size <= 40 * 1024 * 1024:
        # The visual endpoint is instructed to use only video; native ASR owns
        # speech even if the original container happens to include audio.
        return raw_path
    output = analyzer._scratch_dir() / "inline.mp4"
    bitrate = min(4_000_000, int(32 * 1024 * 1024 * 8 / max(1, duration)))
    process = await asyncio.create_subprocess_exec(
        get_ffmpeg_path(),
        "-y",
        "-i",
        str(raw_path),
        "-an",
        "-vf",
        "scale='min(1080,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        str(bitrate),
        "-maxrate",
        str(bitrate),
        "-bufsize",
        str(bitrate * 2),
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await communicate_process(process)
    if process.returncode != 0 or not output.is_file() or output.stat().st_size > 40 * 1024 * 1024:
        raise LLMInvalidOutputError(
            f"Local inline video could not meet its byte budget: {stderr.decode(errors='replace')[-200:]}"
        )
    return output


async def _exec_minimax_chunk(analyzer, raw_path, start, end, actual_start, prompt, query) -> str:
    if end is None or not math.isfinite(end) or end <= start:
        raise LLMInvalidOutputError("MiniMax media requires a closed observed interval")
    duration = await _va.probe_duration(raw_path)
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise LLMInvalidOutputError("The extracted media duration is unavailable")
    actual_end = min(end, actual_start + duration)
    if actual_start > start + 0.05 or actual_end < end - 0.05:
        raise LLMInvalidOutputError("Extracted recording does not cover the requested interval")
    speech = SpeechResult(status="not_requested")
    if analyzer.speech_requirement != "not_requested":
        speech = await _speech_for_chunk(
            analyzer,
            raw_path,
            actual_start,
            required=analyzer.speech_requirement == "required",
            observed_end=actual_end,
        )
    if not analyzer.request_visual:
        raise LLMInvalidOutputError("Audio-only requests must use the native speech tool")
    frames = await _gather_minimax_frames(analyzer, raw_path, start, actual_end, actual_start)
    if not frames:
        raise LLMInvalidOutputError("No visual frames were observed")
    timestamps = [round(actual_start + timestamp, 3) for timestamp, _ in frames]
    facts = {
        "observed_interval": [actual_start, actual_end],
        "sample_timestamps": timestamps,
        "sample_gaps_seconds": [round(b - a, 3) for a, b in zip(timestamps, timestamps[1:])],
        "speech": speech.model_dump(),
        "acoustic": "not_analyzed",
    }
    blocks = [
        {
            "type": "text",
            "text": prompt
            + "\nObserved facts: "
            + json.dumps(facts)
            + "\nOnly report visible evidence; gaps between keyframes remain unknown. Set temporal_evidence_sufficient=false if video playback is needed. Native video does not replace native ASR for speech or analyze non-speech sounds.",
        }
    ]
    for timestamp, data in frames:
        blocks.extend(
            [
                {
                    "type": "text",
                    "text": f"Original recording timestamp: {actual_start + timestamp:.3f}s",
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(data).decode(),
                    },
                },
            ]
        )
    messages = [SystemMessage(content=analyzer.sub_system_prompt), HumanMessage(content=blocks)]
    response = await analyzer._invoke_universal_model(
        messages,
        [_minimax_submit_tool()],
        timeout=analyzer.model_call_timeout_seconds,
        label="MiniMax video keyframes",
    )
    answer = _minimax_answer(response, start, end)
    strategy = "keyframes"
    if not answer.temporal_evidence_sufficient:
        inline = await _inline_video(analyzer, raw_path, duration)
        # Use the same Messages transport/model, replacing the large frame list
        # with native Anthropic video source syntax accepted by the pinned SDK.
        blocks = [
            {
                "type": "text",
                "text": prompt
                + "\nObserved facts: "
                + json.dumps(facts)
                + f"\nPlayback position 0 corresponds to original recording {actual_start:.3f}s. Keyframes were insufficient: {answer.unknowns}. Report remaining unknowns explicitly.",
            },
            {
                "type": "video",
                "source": {
                    "type": "base64",
                    "media_type": "video/mp4",
                    "data": base64.b64encode(inline.read_bytes()).decode(),
                },
                "fps": 5,
                "max_long_side_pixel": 1080,
            },
        ]
        response = await analyzer._invoke_universal_model(
            [SystemMessage(content=analyzer.sub_system_prompt), HumanMessage(content=blocks)],
            [_minimax_submit_tool()],
            timeout=analyzer.model_call_timeout_seconds,
            label="MiniMax native inline video",
        )
        answer = _minimax_answer(response, start, end)
        strategy = "native_inline_video"
    if not answer.temporal_evidence_sufficient:
        raise LLMInvalidOutputError(
            "Temporal evidence remains insufficient after bounded native video analysis"
        )
    coverage = {
        "modality": "visual",
        "start": actual_start,
        "end": actual_end,
        "strategy": strategy,
        "sample_timestamps": timestamps,
        "unknowns": answer.unknowns,
        "evidence": "sampled_stills" if strategy == "keyframes" else "native_video",
    }
    analyzer.media_coverage.append(coverage)
    await _persist_universal_events(
        analyzer,
        [event.model_dump() for event in answer.timeline_events],
        start,
        end,
        actual_start,
        raw_path,
        query,
        answer.summary,
        keyframes=frames,
    )
    # Program-owned coverage/modality facts travel with the legacy child text.
    text = f"[from {start:.1f}s to {end:.1f}s] Summary: {answer.summary} Analysis: {answer.analysis} Media facts: {json.dumps({'coverage': coverage, 'speech': speech.model_dump(), 'acoustic': 'not_analyzed'})}"
    return MediaChunkResult(
        text,
        media_facts={
            "speech": [speech.model_dump()],
            "errors": [
                {
                    "modality": "speech",
                    "category": speech.error_category or speech.status,
                    "error": speech.error,
                    "required": False,
                }
            ]
            if speech.error
            else [],
        },
    )


async def analyze_minimax_speech(analyzer, start, end, query) -> str:
    """Audio-only means required speech; never claim analysis of other sounds."""
    analyzer._assert_recording_source()
    if analyzer.speech_requirement == "not_requested":
        return ToolFailure("Speech was not requested; choose optional/required speech explicitly")
    if not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start):
        return ToolFailure("Audio start time must be finite")
    if end is None:
        start, end, error = await analyzer._resolve_open_ended_interval(start)
        if error:
            return ToolFailure(error)
    if (
        not isinstance(end, (int, float))
        or isinstance(end, bool)
        or not math.isfinite(end)
        or end <= start
    ):
        return ToolFailure("Audio end time must be finite and greater than start")
    controller = _va.get_controller(analyzer.ctx)
    result = await controller.extract_segment_metadata(start, end)
    if not result.success or not result.video_path:
        return ToolFailure("Audio recording extraction failed")
    analyzer._assert_recording_source(result)
    actual_start = result.actual_start_relative_time
    if not isinstance(actual_start, (int, float)):
        actual_start = start
    duration = await _va.probe_duration(Path(result.video_path))
    if actual_start > start + 0.05 or actual_start + duration < end - 0.05:
        return ToolFailure("Audio recording does not cover the requested interval")
    speech = await _speech_for_chunk(
        analyzer,
        Path(result.video_path),
        actual_start,
        required=True,
        observed_end=actual_start + duration,
    )
    analyzer._record_blackboard_entry(
        {
            "start": start,
            "end": end,
            "target": query,
            "summary": json.dumps(speech.model_dump()),
            "confidence_score": 1.0,
        },
        modality="speech",
    )
    return json.dumps({"speech": speech.model_dump(), "acoustic": "not_analyzed"})


async def exec_single_chunk_universal(
    analyzer,
    compressed_path: Path,
    raw_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    prompt_with_context: str,
    specific_query: str,
    force_fallback: bool = False,
) -> str:
    """Executes sub-agent chunk analysis via keyframe extraction and LangChain ChatModel."""
    if analyzer.is_minimax:
        return await _exec_minimax_chunk(
            analyzer,
            raw_path,
            start_time,
            end_time,
            actual_start,
            prompt_with_context,
            specific_query,
        )
    keyframes = _gather_universal_frames(analyzer, raw_path, start_time, end_time, actual_start)

    user_blocks, audio_block = await _build_universal_user_blocks(
        analyzer, keyframes, actual_start, prompt_with_context, raw_path
    )

    messages = [
        SystemMessage(content=analyzer.sub_system_prompt),
        HumanMessage(content=user_blocks),
    ]

    trace_id = CURRENT_TRACE_ID.get()
    if analyzer.ctx.data_engine and trace_id:
        analyzer.ctx.data_engine.record_trace(
            type="llm_call",
            name="video_sub_agent_universal",
            payload={
                "num_keyframes": len(keyframes),
                "prompt": prompt_with_context,
                "system_instruction": analyzer.sub_system_prompt,
            },
            parent_trace_id=trace_id,
        )

    response = await _invoke_universal_with_visual_retry(
        analyzer, messages, user_blocks, audio_block, force_fallback
    )

    final_summary, final_analysis, timeline_events = _extract_submit_payload(response)

    await _persist_universal_events(
        analyzer,
        timeline_events,
        start_time,
        end_time,
        actual_start,
        raw_path,
        specific_query,
        final_summary,
    )

    end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
    return (
        f"[from {start_time:.1f}s to {end_str}] Summary: {final_summary} Analysis: {final_analysis}"
    )


def _gather_universal_frames(
    analyzer,
    raw_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    *,
    cancel_event: Event | None = None,
    deadline: float | None = None,
) -> list:
    """Extracts uniform keyframes plus dense action-window frames, merged by timestamp."""
    # Universal providers consume stills rather than the uploaded clip. Use
    # the raw extracted segment so slowdown/transcode PTS never skew labels.
    if analyzer.is_minimax:
        duration = max(0.0, (end_time or start_time) - actual_start)
        # Include the tail within the last decoded frame, rather than stopping
        # after 30 seconds of a longer recording. Dense action samples are extra.
        last = max(0.0, duration - min(0.5, duration / 2))
        offsets = [last * index / 29 for index in range(30)]
        offsets.extend(analyzer._dense_action_offsets(start_time, end_time, actual_start))
        return _va.extract_frames_at_timestamps(
            raw_path,
            sorted(set(offsets)),
            max_frames=30 + analyzer.max_dense_action_frames,
            max_dimension=1080,
            cancel_event=cancel_event,
            deadline=deadline,
        )
    else:
        keyframes = _va.extract_keyframes_from_video(
            raw_path, fps=1.0, max_frames=30, max_dimension=1080
        )
    dense_offsets = analyzer._dense_action_offsets(
        start_time,
        end_time if end_time is not None else start_time,
        actual_start,
    )
    targeted_frames = _va.extract_frames_at_timestamps(
        raw_path,
        dense_offsets,
        max_frames=analyzer.max_dense_action_frames,
        max_dimension=1080,
    )
    merged_frames = {round(timestamp, 3): data for timestamp, data in keyframes}
    merged_frames.update({round(timestamp, 3): data for timestamp, data in targeted_frames})
    return sorted(merged_frames.items())


async def _gather_minimax_frames(
    analyzer, path, start, end, actual_start, *, timestamps=None
) -> list:
    """Keep CPU decode cancellable and join it before owned files are removed."""
    stop = Event()
    with logical_call_scope() as budget:
        deadline = time.monotonic() + budget.remaining_seconds

        def decode():
            if timestamps is not None:
                return _va.extract_frames_at_timestamps(
                    path,
                    timestamps,
                    max_frames=len(timestamps),
                    max_dimension=1080,
                    cancel_event=stop,
                    deadline=deadline,
                )
            return _gather_universal_frames(
                analyzer, path, start, end, actual_start, cancel_event=stop, deadline=deadline
            )

        task = asyncio.create_task(asyncio.to_thread(decode))
        try:
            return await asyncio.shield(task)
        except BaseException:
            stop.set()
            try:
                await drain_media_task(task)
            except BaseException as cleanup_error:
                logger.debug(
                    f"Frame worker stopped during cleanup ({type(cleanup_error).__name__})"
                )
            raise


async def _build_universal_user_blocks(
    analyzer,
    keyframes: list,
    actual_start: float,
    prompt_with_context: str,
    raw_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Builds the multimodal user content blocks (text, frames, optional audio)."""
    user_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_with_context}]

    for ts_sec, frame_bytes in keyframes:
        b64_str = base64.b64encode(frame_bytes).decode("utf-8")
        abs_time = actual_start + ts_sec
        user_blocks.append(
            {
                "type": "text",
                "text": f"--- Video Keyframe at {abs_time:.1f}s ---",
            }
        )
        user_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"},
            }
        )

    audio_block = None
    try:
        async with _va.TRANSCODE_SEMAPHORE:
            audio_path = await _va.extract_audio_from_video(raw_path)
        analyzer.local_files_to_cleanup.add(audio_path)
        audio_block = analyzer._audio_content_block(audio_path)
        user_blocks.append(audio_block)
    except Exception as audio_error:
        logger.info(f"No usable audio track for universal video chunk: {audio_error}")

    return user_blocks, audio_block


async def _invoke_universal_with_visual_retry(
    analyzer,
    messages: list[BaseMessage],
    user_blocks: list[dict[str, Any]],
    audio_block: dict[str, Any] | None,
    force_fallback: bool,
):
    """Invokes the universal model, retrying visually when audio input is rejected."""
    with TraceSpan(name="universal_sub_agent_call") as span:
        try:
            response = await analyzer._invoke_universal_model(
                messages,
                [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
                timeout=analyzer.model_call_timeout_seconds,
                label="Universal video sub-agent",
                force_fallback=force_fallback,
            )
        except Exception as multimodal_error:
            failure = classify_video_failure(multimodal_error)
            if audio_block is None or failure.category.value not in {
                "bad_request",
                "media_processing",
            }:
                raise
            logger.warning(
                "Configured video model rejected audio input; retrying this chunk visually"
            )
            visual_blocks = [block for block in user_blocks if block is not audio_block]
            messages = [
                SystemMessage(content=analyzer.sub_system_prompt),
                HumanMessage(content=visual_blocks),
            ]
            response = await analyzer._invoke_universal_model(
                messages,
                [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
                timeout=analyzer.model_call_timeout_seconds,
                label="Universal visual-only sub-agent",
                force_fallback=force_fallback,
            )
        span.result = f"Received response (tool_calls={len(getattr(response, 'tool_calls', []))})"
    return response


def _extract_submit_payload(response) -> tuple[str, str, list]:
    """Extract a summary, analysis, and events from the model response.

    Use the first submit_answer call if its summary is non-empty. Otherwise,
    fall back to response text, using its first 200 characters as the summary
    and dropping any events from the rejected call. Raise SubAgentAnswerExhausted
    if neither source provides an answer.
    """
    for tc in getattr(response, "tool_calls", None) or []:
        if tc.get("name") != "submit_answer":
            continue
        args = tc.get("args") or {}
        value = args.get("summary", "")
        final_summary = value.strip().replace("\n", " ") if isinstance(value, str) else ""
        if not final_summary:
            break
        final_analysis = (
            str(args.get("analysis", "")).strip().replace("\n", " ") or "No analysis provided."
        )
        return final_summary, final_analysis, list(args.get("timeline_events") or [])

    raw_content = getattr(response, "content", None)
    text_content = content_to_text(raw_content).strip() if raw_content else ""
    if text_content:
        return text_content[:200].replace("\n", " "), text_content, []

    raise SubAgentAnswerExhausted(
        1, "the model neither called submit_answer with a summary nor replied with text"
    )


async def _persist_universal_events(
    analyzer,
    timeline_events: list,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    raw_path: Path,
    specific_query: str,
    final_summary: str,
    *,
    keyframes: list | None = None,
) -> None:
    """Processes timeline events & saves screenshots for the blackboard ledger."""
    if analyzer.is_minimax:
        await _persist_minimax_events(
            analyzer,
            timeline_events,
            actual_start,
            raw_path,
            specific_query,
            final_summary,
            keyframes or [],
        )
        return
    for event in timeline_events:
        if not isinstance(event, dict):
            continue
        try:
            v_ts = float(event.get("verification_timestamp_secs", start_time))
            conf = float(event.get("confidence_score", 0.8))
            event_start = float(event.get("start_time", start_time))
            event_end = float(
                event.get(
                    "end_time",
                    end_time if end_time is not None else event_start,
                )
            )
        except (TypeError, ValueError):
            logger.warning(f"Ignoring malformed universal video event: {event}")
            continue
        interval_end = end_time if end_time is not None else max(event_end, v_ts)
        if (
            not 0.0 <= conf <= 1.0
            or event_end < event_start
            or event_start < start_time
            or event_end > interval_end
            or v_ts < start_time
            or v_ts > interval_end
        ):
            logger.warning(f"Ignoring out-of-range universal video event: {event}")
            continue

        screenshot_path_str = None
        try:
            rel_offset = max(0.0, v_ts - actual_start)
            out_dir = (
                analyzer._scratch_dir()
                if analyzer.is_minimax
                else _va.get_temp_dir("video_analyzer")
            )
            out_dir.mkdir(parents=True, exist_ok=True)

            existing_imgs = glob.glob(f"{out_dir}/img_*.jpg")
            max_idx = -1
            for img in existing_imgs:
                m = re.search(r"img_(\d+)\.jpg$", img)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
            next_idx = max_idx + 1
            shot_path = out_dir / f"img_{next_idx}.jpg"

            ffmpeg_cmd = [
                get_ffmpeg_path(),
                "-ss",
                str(rel_offset),
                "-i",
                str(raw_path),
                "-vframes",
                "1",
                "-q:v",
                "2",
                "-y",
                str(shot_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await communicate_process(proc)
            if shot_path.exists():
                analyzer.local_files_to_cleanup.add(shot_path)
                screenshot_path_str = str(shot_path)
        except Exception as fe:
            logger.warning(f"Failed extracting keyframe for event {v_ts}s: {fe}")

        entry = {
            "start": event_start,
            "end": event_end,
            "target": specific_query,
            "summary": event.get("transcription", final_summary),
            "confidence_score": conf,
        }
        if screenshot_path_str:
            entry["screenshot"] = screenshot_path_str
        analyzer._record_blackboard_entry(entry)


async def _persist_minimax_events(analyzer, events, actual_start, path, query, summary, frames):
    """Persist observed PTS and durable evidence, never a seek request as fact."""
    requested = [event["verification_timestamp_secs"] - actual_start for event in events]
    missing = [
        offset for offset in requested if not any(abs(ts - offset) < 0.001 for ts, _ in frames)
    ]
    if missing:
        frames = [
            *frames,
            *await _gather_minimax_frames(
                analyzer, path, 0, 0, actual_start, timestamps=sorted(set(missing))
            ),
        ]
    for index, event in enumerate(events):
        offset = event["verification_timestamp_secs"] - actual_start
        choices = [(ts, data) for ts, data in frames if ts + 0.001 >= offset]
        entry = {
            "start": event["start_time"],
            "end": event["end_time"],
            "target": query,
            "summary": event.get("transcription", summary),
            "confidence_score": event["confidence_score"],
            "requested_verification_timestamp_secs": event["verification_timestamp_secs"],
        }
        if choices:
            observed, data = min(choices, key=lambda pair: pair[0])
            shot = analyzer._scratch_dir() / f"event_{index}.jpg"
            shot.write_bytes(data)
            if analyzer.blackboard.evidence_dir is None:
                # Reuse the Blackboard's evidence owner even without tracing.
                # This persistent directory is never registered as run scratch.
                from hashlib import sha256

                root = (
                    _va.get_temp_dir("video_analyzer")
                    / "evidence"
                    / sha256(analyzer.blackboard.board_key.encode()).hexdigest()[:24]
                )
                root.mkdir(parents=True, exist_ok=True)
                analyzer.blackboard.evidence_dir = root
            entry.update(
                {
                    "screenshot": str(shot),
                    "verification_timestamp_secs": round(actual_start + observed, 3),
                    "verification_evidence": "decoded_frame",
                    "observed_frame_interval": [round(actual_start + observed, 3)] * 2,
                }
            )
        else:
            entry["verification_evidence"] = "unavailable"
            entry["verification_unknown"] = (
                "No decoded frame was available at/after the requested verification time"
            )
        analyzer._record_blackboard_entry(entry)


async def exec_analyze_audio_universal(
    analyzer,
    audio_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    prompt_with_context: str,
    specific_query: str,
    force_fallback: bool = False,
) -> str:
    """Executes audio analysis via Universal LangChain ChatModel."""
    messages = [
        SystemMessage(content=analyzer.audio_system_prompt),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt_with_context},
                analyzer._audio_content_block(audio_path),
            ]
        ),
    ]
    response = await analyzer._invoke_universal_model(
        messages,
        [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
        timeout=analyzer.model_call_timeout_seconds,
        label="Universal audio sub-agent",
        force_fallback=force_fallback,
    )
    # Same resolution order as the video chunk (raises SubAgentAnswerExhausted
    # when the model produced nothing usable); audio has no timeline events.
    final_summary, final_analysis, _ = _extract_submit_payload(response)

    end_val = end_time if end_time is not None else "unknown"
    analyzer._record_blackboard_entry(
        {
            "start": start_time,
            "end": end_val,
            "target": specific_query,
            "summary": final_summary,
            "confidence_score": 0.8,
        },
        modality="audio",
    )

    end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
    return (
        f"[from {start_time:.1f}s to {end_str}] Audio Summary: {final_summary}"
        f" Analysis: {final_analysis}"
    )


async def run_universal(
    analyzer,
    time_description: str,
    purpose: str,
    system_prompt: str,
    *,
    force_fallback: bool = False,
) -> tuple[str, str]:
    """Runs VideoAnalyzer main reasoning loop using Universal LangChain BaseChatModel."""
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=(
                f"Time description: {time_description}\n"
                f"Purpose: {purpose}\n"
                f"Persistent video blackboard: {analyzer.blackboard_ledger}\n"
                "Reuse sufficient prior evidence and do not request an identical "
                "interval/query analysis again.\n"
                "Please coordinate video metadata extraction and sub-agent analysis."
                + (
                    f"\nOriginal purpose: {purpose}. Requested scope: {analyzer.requested_scope.model_dump_json() if analyzer.requested_scope else 'unresolved'}. Requested visual={analyzer.request_visual}, speech={analyzer.speech_requirement}, acoustic=unsupported. Visual chunks must use spawn_sub_agent; audio-only uses analyze_audio_only. Each required modality must cover the entire requested scope. Report actual evidence/coverage/unknowns."
                    if analyzer.is_minimax
                    else ""
                )
            )
        ),
    ]

    max_iterations = 6
    iterations = 0
    agent_outcome = ""
    status = "success"
    tool_failed = False
    caught_error = False

    try:
        while iterations < max_iterations:
            iterations += 1

            async def call():
                return await analyzer._invoke_universal_model(
                    messages,
                    _va.UNIVERSAL_MAIN_TOOLS,
                    timeout=300,
                    label="Universal video coordinator",
                    force_fallback=force_fallback,
                )

            response = (
                await call()
                if analyzer.is_minimax
                else await _va._invoke_with_retry(call, "Universal video coordinator")
            )
            messages.append(response)

            if not getattr(response, "tool_calls", None):
                agent_outcome = content_to_text(response.content).strip()
                break

            for tc in response.tool_calls:
                t_name = tc["name"]
                t_args = tc.get("args", {})
                t_id = tc.get("id", f"call_{iterations}")
                logger.info(f"Executing universal tool '{t_name}' with args {t_args}")

                res = await _execute_universal_tool(analyzer, t_name, t_args)
                tool_failed = tool_failed or is_tool_failure(res)
                if is_tool_failure(res):
                    analyzer.media_errors.append(
                        {"tool": t_name, "category": "subtool_failure", "error": str(res)}
                    )

                messages.append(
                    ToolMessage(
                        tool_call_id=t_id,
                        name=t_name,
                        content=str(res),
                        status="error" if is_tool_failure(res) else "success",
                    )
                )

        if not agent_outcome:
            agent_outcome = "Video analyzer exhausted turns without a usable final answer."
            status = "error"
        if tool_failed:
            status = "error"

    except Exception as e:
        logger.error(f"Universal video analyzer failed: {e}")
        agent_outcome = f"Video analysis failed: {e}"
        status = "error"
        caught_error = True
        analyzer.media_errors.append(
            {"category": classify_failure(e).category.value, "error": str(e)}
        )

    analyzer._record_reliability_metrics()
    if analyzer.is_minimax:
        return analyzer._finalize_media_result(
            agent_outcome, successful=status == "success", failed=caught_error
        ).legacy()
    return agent_outcome, status


async def _execute_universal_tool(analyzer, t_name: str, t_args: dict) -> str:
    """Dispatches one universal coordinator tool call, converting errors to text."""
    try:
        if t_name == "extract_segment_metadata":
            res = await analyzer.exec_extract_segment_metadata(
                t_args.get("start_time"), t_args.get("end_time")
            )
        elif t_name == "spawn_sub_agent":
            res = await analyzer.exec_spawn_sub_agent(
                t_args.get("start_time"),
                t_args.get("end_time"),
                t_args.get("specific_query"),
            )
        elif t_name == "analyze_audio_only":
            res = await analyzer.exec_analyze_audio_only(
                t_args.get("start_time"),
                t_args.get("end_time"),
                t_args.get("specific_query"),
            )
        else:
            res = ToolFailure(f"Error: Tool '{t_name}' is not recognized.")
    except Exception as tool_err:
        logger.error(f"Error executing tool '{t_name}': {tool_err}")
        failure = classify_failure(tool_err)
        analyzer.media_errors.append(
            {"tool": t_name, "category": failure.category.value, "error": str(tool_err)}
        )
        res = ToolFailure(f"Error executing tool '{t_name}': {tool_err}")
    return res
