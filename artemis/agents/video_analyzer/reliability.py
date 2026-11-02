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

"""Failure classification and circuit breaking for video model calls.

Thin media-aware extension of :mod:`artemis.llm.reliability`, which owns the
generic taxonomy and circuit breaker.  This module adds the media-processing
category and the ``should_split`` recovery decision (bisecting long clips).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from artemis.llm.reliability import (
    CircuitBreaker,
    FailureCategory,
    classify_failure,
    extract_status_code,
)


class VideoFailureCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    MEDIA_PROCESSING = "media_processing"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    INVALID_OUTPUT = "invalid_output"
    BALANCE = "balance"
    PERMISSION = "permission"
    CONTENT_REFUSAL = "content_refusal"
    QUOTA_EXHAUSTED = "quota_exhausted"


class RequestedMediaScope(BaseModel):
    """A user interval bound before coordination, independent of tool work."""

    time_description: str
    resolution: Literal["explicit", "recording_metadata", "unresolved"] = "unresolved"
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error: str | None = None

    @property
    def interval(self) -> tuple[float, float] | None:
        if (
            self.resolution != "unresolved"
            and self.start is not None
            and self.end is not None
            and self.end > self.start
        ):
            return self.start, self.end
        return None


class MediaCoverage(BaseModel):
    """Successful evidence coverage for exactly one observed modality."""

    model_config = ConfigDict(extra="allow", frozen=True)
    modality: Literal["visual", "speech"]
    start: float = Field(strict=True, ge=0, allow_inf_nan=False)
    end: float = Field(strict=True, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _valid_interval(self):
        if self.end <= self.start:
            raise ValueError("Media coverage end must exceed start")
        return self


class MediaAnalysisResult(BaseModel):
    """Internal result; only complete is exposed as legacy tuple success."""

    status: Literal["complete", "partial", "incomplete", "failed"]
    outcome: str
    requested_scope: RequestedMediaScope | None = None
    coverage: list[MediaCoverage] = Field(default_factory=list)
    missing_coverage: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    modalities: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    recording_source: dict[str, Any] = Field(default_factory=dict)

    def legacy(self) -> tuple[str, str]:
        return self.model_dump_json(), "success" if self.status == "complete" else "error"


class MediaChunkResult(str):
    """Legacy child text carrying program-owned facts to the Blackboard commit."""

    media_facts: dict[str, Any]

    def __new__(cls, text: str, *, media_facts: dict[str, Any]):
        value = super().__new__(cls, text)
        value.media_facts = media_facts
        return value


@dataclass(frozen=True)
class VideoFailure:
    category: VideoFailureCategory
    retryable: bool
    should_split: bool
    should_fallback: bool


_MEDIA_MARKERS = ("ffmpeg", "codec", "corrupt", "invalid video", "invalid audio", "media")
_AGENTIC_REJECTION_MARKERS = ("processing", "agentic")


class SubAgentAnswerExhausted(RuntimeError):
    """The sub-agent exhausted its turns without a valid submit_answer.

    The caller records a failed segment so a later claim can retry it. The shared
    classifier permits retries, splitting, and fallback for this failure.
    """

    def __init__(self, turns: int, reason: str):
        self.turns = turns
        self.reason = reason
        super().__init__(
            f"submit_answer was not accepted within {turns} turn(s); last rejection: {reason}"
        )


class AgenticVideoDegraded(RuntimeError):
    """Return an interval to the coordinator after switching to static mode.

    The coordinator splits oversized clips and retries smaller clips as-is.
    """


def is_agentic_rejection(failure: VideoFailure, error: BaseException) -> bool:
    """True when a bad request is the API refusing agentic video processing.

    Only an HTTP 400 whose message mentions ``processing`` / ``agentic``
    qualifies; other bad requests (413 payload too large, 404, ...) keep the
    shared handling.
    """
    if failure.category is not VideoFailureCategory.BAD_REQUEST:
        return False
    if extract_status_code(error) != 400:
        return False
    message = str(error).lower()
    return any(marker in message for marker in _AGENTIC_REJECTION_MARKERS)


def classify_video_failure(error: BaseException) -> VideoFailure:
    """Convert provider/transport/media exceptions into recovery decisions."""
    if isinstance(error, AgenticVideoDegraded):
        return VideoFailure(VideoFailureCategory.BAD_REQUEST, False, True, False)
    generic = classify_failure(error)
    message = str(error).lower()
    has_media_marker = any(marker in message for marker in _MEDIA_MARKERS)

    # Media markers take precedence over generic connection / bad-request /
    # unknown classification (rate-limit, unavailable, timeout, and auth
    # signals still win, matching the original ordering of checks).
    if (
        not hasattr(error, "failure")
        and has_media_marker
        and generic.category
        in {
            FailureCategory.CONNECTION,
            FailureCategory.BAD_REQUEST,
            FailureCategory.UNKNOWN,
        }
    ):
        # Media failures are about the extracted clip itself (missing frames,
        # corrupt container, unsupported codec). Bisecting the interval only
        # re-runs the same broken extraction on smaller ranges and turns one
        # failure into a cascade of them, so the chunk fails fast instead.
        return VideoFailure(VideoFailureCategory.MEDIA_PROCESSING, False, False, True)

    category = VideoFailureCategory(generic.category.value)
    should_split = category in {VideoFailureCategory.TIMEOUT, VideoFailureCategory.UNKNOWN} or (
        category is VideoFailureCategory.BAD_REQUEST and extract_status_code(error) == 413
    )
    # The video pipeline keeps its historical behavior of falling back to the
    # universal analyzer even for bad requests (a different provider may accept
    # the same media payload).
    should_fallback = generic.should_fallback or (
        category is VideoFailureCategory.BAD_REQUEST and not hasattr(error, "failure")
    )
    if category is VideoFailureCategory.CANCELLED:
        should_fallback = False
    return VideoFailure(category, generic.retryable, should_split, should_fallback)


class VideoCircuitBreaker(CircuitBreaker):
    """Context-scoped circuit breaker keyed by provider/model identity."""
