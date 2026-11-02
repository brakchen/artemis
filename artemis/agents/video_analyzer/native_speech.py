"""Native MiniMax ASR, using task credentials and the existing recovery owner.

https://platform.minimaxi.com/docs/api-reference/speech-to-text.md
httpx 0.28.1 (BSD-3-Clause) owns multipart encoding and boundary generation.
No Files upload, Google client, text-model fallback, or synthetic token usage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
from pathlib import Path
import time
from typing import Callable, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from artemis.llm.minimax import normalize_minimax_error, raise_for_minimax_error
from artemis.llm.minimax_media import media_input_error
from artemis.llm.reliability import LLMInvalidOutputError
from artemis.services.llm import _record_llm_event, _run_with_recovery
from artemis.utils.video import communicate_process, drain_media_task, get_ffmpeg_path
from third_party.mobile_use.utils.video import (
    NoAudioTrackError,
    extract_audio_from_video,
    probe_duration,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

ASR_BODY_LIMIT = 52_428_800
ASR_MODEL = "asr-1.0"


async def _close_asr_resource(resource, original_error: BaseException | None) -> None:
    try:
        await drain_media_task(asyncio.create_task(resource.aclose()))
    except asyncio.CancelledError:
        if original_error is None:
            raise
        logger.debug("ASR cleanup drained repeated cancellation; preserving original failure")
    except (httpx.HTTPError, OSError, RuntimeError) as cleanup_error:
        logger.debug(f"Native ASR cleanup failed ({type(cleanup_error).__name__})")


class SpeechResult(BaseModel):
    status: Literal[
        "transcribed", "no_speech", "no_track", "failed", "unsupported", "not_requested"
    ]
    segments: list[dict] = Field(default_factory=list)
    coverage: list[dict] = Field(default_factory=list)
    error: str | None = None
    error_category: str | None = None
    # Speaker labels, if any, are local to one HTTP request.
    speaker_scope: str = "request_local"


def native_base_for(endpoint) -> str | None:
    """Map only known official Messages origins; never export gateway keys."""
    parsed = urlsplit(str(endpoint.api_base or ""))
    hosts = {"api.minimax.cn", "api.minimax.io"}
    if (
        endpoint.vendor != "minimax"
        or parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/anthropic", "/anthropic/v1"}
    ):
        return None
    return f"https://{parsed.hostname}/v1"


async def _audio_slice(source: Path, output: Path, offset: float, duration: float) -> None:
    process = await asyncio.create_subprocess_exec(
        get_ffmpeg_path(),
        "-y",
        "-ss",
        str(offset),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await communicate_process(process)
    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg ASR extraction failed: {stderr.decode(errors='replace')[-500:]}"
        )


def _segments(payload, *, offset: float, duration: float, request_id: int) -> list[dict]:
    raw = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise LLMInvalidOutputError("MiniMax verbose ASR response has no aligned segments")
    segments = []
    for value in raw:
        if not isinstance(value, dict):
            raise LLMInvalidOutputError("MiniMax ASR returned an invalid segment")
        start, end, text = value.get("start"), value.get("end"), value.get("text")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start <= end <= duration + 0.1
            or not isinstance(text, str)
        ):
            raise LLMInvalidOutputError("MiniMax ASR segment timing/text is invalid")
        if text.strip():
            item = {
                "start": round(offset + start, 3),
                "end": round(offset + end, 3),
                "text": text.strip(),
                "request_id": request_id,
            }
            speaker = value.get("speaker_id", value.get("speaker"))
            if isinstance(speaker, (str, int)):
                item["speaker"] = str(speaker)
                item["speaker_scope"] = f"request_{request_id}"
            segments.append(item)
    return segments


async def transcribe_audio(
    endpoint,
    audio_path: Path,
    *,
    actual_start: float,
    work_dir: Path,
    duration: float | None = None,
    language: str | None = None,
    client: httpx.AsyncClient | None = None,
    source_check: Callable[[], None] | None = None,
) -> SpeechResult:
    native_base = native_base_for(endpoint)
    if native_base is None:
        return SpeechResult(
            status="unsupported", error="Native ASR is unsupported for this Messages gateway"
        )
    if not endpoint.api_key:
        return SpeechResult(
            status="failed", error="Selected MiniMax task credential is unavailable"
        )
    duration = duration if duration is not None else await probe_duration(audio_path)
    if not math.isfinite(duration) or duration <= 0:
        raise LLMInvalidOutputError("Audio duration is unavailable")
    work_dir.mkdir(parents=True, exist_ok=True)
    url = f"{native_base}/speech_to_text"
    target = (
        "minimax:asr:"
        + hashlib.sha256(repr((native_base, ASR_MODEL, endpoint.api_key)).encode()).hexdigest()[:24]
    )
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=endpoint.timeout_seconds, follow_redirects=False)
    result = SpeechResult(status="no_speech")
    offset = 0.0
    request_id = 0
    try:
        while offset < duration:
            request_id += 1
            length = min(480.0, duration - offset)
            path = audio_path
            if duration > 500.0 or audio_path.stat().st_size > ASR_BODY_LIMIT - 4096:
                path = work_dir / f"asr_{request_id}.mp3"
                await _audio_slice(audio_path, path, offset, length)
            else:
                length = duration

            async def call_once():
                if source_check is not None:
                    source_check()
                # Reading inside each attempt closes the handle before a retry,
                # cancellation, or scratch cleanup. httpx owns multipart bytes.
                fields = {
                    "model": ASR_MODEL,
                    "response_format": "verbose_json",
                    "stream": "false",
                    "timestamp_level": "sentence",
                }
                headers = {"Authorization": f"Bearer {endpoint.api_key}"}
                if language:
                    headers["language"] = language
                with path.open("rb") as stream:
                    request = client.build_request(
                        "POST",
                        url,
                        headers=headers,
                        data=fields,
                        files={"file": (path.name, stream, "audio/mpeg")},
                    )
                    body_length = int(request.headers.get("Content-Length", ASR_BODY_LIMIT + 1))
                    if body_length > ASR_BODY_LIMIT:
                        raise media_input_error(
                            "MiniMax ASR complete multipart body exceeds 50 MiB"
                        )
                    started = time.monotonic()
                    try:
                        async with asyncio.timeout(endpoint.timeout_seconds):
                            response = await client.send(request, follow_redirects=False)
                    except httpx.HTTPError as exc:
                        raise normalize_minimax_error(error=exc) or exc
                try:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise_for_minimax_error(status_code=response.status_code, payload=None)
                        raise LLMInvalidOutputError("MiniMax ASR returned invalid JSON") from exc
                    raise_for_minimax_error(payload, status_code=response.status_code)
                    if response.status_code != 200:
                        raise media_input_error("MiniMax ASR returned an unsupported HTTP response")
                    segments = _segments(
                        payload,
                        offset=actual_start + offset,
                        duration=length,
                        request_id=request_id,
                    )
                    _record_llm_event(
                        "native_asr",
                        {
                            "model": ASR_MODEL,
                            "duration_seconds": length,
                            "elapsed_seconds": time.monotonic() - started,
                            "segment_count": len(segments),
                            "usage": payload.get("usage"),
                        },
                    )
                    return segments
                finally:
                    await _close_asr_resource(response, sys.exception())

            values = await _run_with_recovery(
                call_once, provider="minimax", endpoint_key=target, minimax_target=target
            )
            for value in values:
                # Only overlapping recordings of the same utterance deduplicate;
                # identical text spoken at a different time remains evidence.
                if not any(
                    old["text"] == value["text"]
                    and old["request_id"] != value["request_id"]
                    and old["start"] < value["end"]
                    and value["start"] < old["end"]
                    for old in result.segments
                ):
                    result.segments.append(value)
            result.coverage.append(
                {
                    "start": actual_start + offset,
                    "end": actual_start + offset + length,
                    "evidence": "native_asr",
                    "request_id": request_id,
                }
            )
            if offset + length >= duration:
                break
            offset += length - 1.0
        result.status = "transcribed" if result.segments else "no_speech"
        return result
    finally:
        if owned_client:
            await _close_asr_resource(client, sys.exception())


async def transcribe_video(analyzer, path: Path, actual_start: float) -> SpeechResult:
    analyzer._assert_recording_source()
    if native_base_for(analyzer.endpoint) is None:
        return SpeechResult(
            status="unsupported", error="Native ASR is unsupported for this Messages gateway"
        )
    work_dir = analyzer._scratch_dir()
    audio_path = work_dir / "speech.mp3"
    try:
        await extract_audio_from_video(path, output_path=audio_path)
    except NoAudioTrackError:
        return SpeechResult(status="no_track")
    analyzer._assert_recording_source()
    return await transcribe_audio(
        analyzer.endpoint,
        audio_path,
        actual_start=actual_start,
        work_dir=work_dir,
        language=getattr(analyzer.video_config, "speech_language", None),
        source_check=analyzer._assert_recording_source,
    )
