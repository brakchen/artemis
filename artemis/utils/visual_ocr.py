"""Typed MiniMax OCR, deterministic tiles, and versioned ImageRecord caching.

The legacy Google HTTP boundary remains in ocr_api. This module only uses the
task's default visual model. Exceptions remain exceptions, never confirmed empty.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, model_validator

from artemis.llm.reliability import logical_call_scope
from artemis.services.llm import get_effective_endpoint, get_llm
from artemis.utils.coordinates import denormalize_bbox
from artemis.utils.vision import (
    COORDINATE_VERSION,
    ImageTransform,
    SnapshotRef,
    provenance,
    snapshot_for_image,
)
from artemis.utils.vision_models import OCRModelOutput, OCRRegion
from artemis.utils.vision_requests import complete_visual

OCR_PROMPT_VERSION = "minimax-ocr-v1"
OCR_SCHEMA_VERSION = "regions-ltrb-v1"
OCR_PROMPT = (
    "Transcribe only text actually visible in this image. Return status complete, partial, or empty "
    "and regions with text, bbox=[left,top,right,bottom], and uncertain. Coordinates are x,y on "
    "the actual input image's 0..1000 grid. Preserve original spelling and language. For illegible "
    "characters use the replacement character � and uncertain=true; do not infer missing letters "
    "from context. Mark partial for uncertain or incomplete recognition; empty only when no text "
    "is visible. complete describes completion of this request, not guaranteed recognition accuracy."
)


class OCRResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["complete", "partial", "empty"]
    regions: list[OCRRegion]
    snapshot: SnapshotRef
    image_size: tuple[int, int]
    source: Literal["minimax"] = "minimax"
    prompt_version: Literal["minimax-ocr-v1"] = OCR_PROMPT_VERSION
    schema_version: Literal["regions-ltrb-v1"] = OCR_SCHEMA_VERSION
    coordinate_version: Literal["xy-ltrb-1000-v1"] = COORDINATE_VERSION
    transform: ImageTransform = ImageTransform()
    cache_key: str
    cache_hit: bool = False

    @model_validator(mode="after")
    def _validate_status(self):
        OCRModelOutput.model_validate({"status": self.status, "regions": self.regions})
        return self


def ocr_to_legacy(result: OCRResult) -> list[dict]:
    """Pixel polygon adapter. No status-bar or root-image offset is applied."""
    output = []
    for region in result.regions:
        left, t, r, b = denormalize_bbox(region.bbox, *result.image_size)
        output.append(
            {
                "text": region.text,
                "position": [
                    {"x": round(left), "y": round(t)},
                    {"x": round(r), "y": round(t)},
                    {"x": round(r), "y": round(b)},
                    {"x": round(left), "y": round(b)},
                ],
                **provenance(
                    result.snapshot,
                    source=result.source,
                    bbox=result.transform.map_bbox(
                        region.bbox,
                        result.image_size,
                        (result.snapshot.width, result.snapshot.height),
                    ),
                    transform=result.transform,
                    status=result.status,
                ),
                "input_bbox": region.bbox,
                "position_space": "input_pixel",
                "ocr_status": result.status,
                "ocr_version": result.schema_version,
                "recognition_source": result.source,
                "uncertain": region.uncertain,
            }
        )
    return output


def tile_bounds(
    width: int, height: int, *, edge: int = 1600, overlap: int = 160
) -> list[tuple[int, int, int, int]]:
    """Deterministic overlapping tiles for high resolution/dense and long images."""

    def starts(length):
        if length <= edge:
            return [0]
        points = list(range(0, length - edge + 1, edge - overlap))
        if points[-1] != length - edge:
            points.append(length - edge)
        return points

    return [
        (x, y, min(width, x + edge), min(height, y + edge))
        for y in starts(height)
        for x in starts(width)
    ]


def _overlap(a: list[float], b: list[float]) -> float:
    left, t, r, bot = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if r <= left or bot <= t:
        return 0
    return (
        (r - left) * (bot - t) / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    )


def _merge(regions: list[OCRRegion]) -> list[OCRRegion]:
    # Only identical text in the same overlapping area is deduplicated. Different
    # readings stay visible as uncertainty rather than choosing a model answer.
    merged: list[OCRRegion] = []
    for region in regions:
        existing = next(
            (r for r in merged if r.text == region.text and _overlap(r.bbox, region.bbox) > 0.6),
            None,
        )
        if existing is None:
            merged.append(region)
        elif region.uncertain:
            merged[merged.index(existing)] = existing.model_copy(update={"uncertain": True})
    for i, region in enumerate(merged):
        if any(
            other.text != region.text and _overlap(other.bbox, region.bbox) > 0.8
            for other in merged
        ):
            merged[i] = region.model_copy(update={"uncertain": True})
    return sorted(merged, key=lambda r: (r.bbox[1], r.bbox[0], r.text))


def ocr_cache_key(ctx, image_data: bytes, *, transform=None) -> str:
    endpoint = get_effective_endpoint(ctx, "default")
    payload = {
        "image_hash": hashlib.sha256(image_data).hexdigest(),
        "endpoint": endpoint.model_dump(mode="json"),
        # Also separates credential/config identities without persisting secrets.
        "endpoint_identity": endpoint.identity_key(),
        "prompt": OCR_PROMPT_VERSION,
        "schema": OCR_SCHEMA_VERSION,
        "coordinates": COORDINATE_VERSION,
        "transform": (transform or ImageTransform()).model_dump(),
        "tile_edge": 1600,
        "overlap": 160,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class _InFlightOCR:
    task: asyncio.Task
    waiters: int = 0


def _storage(ctx):
    engine = getattr(ctx, "data_engine", None)
    storage = getattr(engine, "storage", None)
    # Legacy MagicMock engines must not manufacture cache hits or successful writes.
    from artemis.data_engine.storage import StorageManager

    return (engine, storage) if isinstance(storage, StorageManager) else (None, None)


async def recognize_ocr(
    screenshot_b64: str,
    *,
    ctx,
    snapshot: SnapshotRef | None = None,
    transform: ImageTransform | None = None,
    llm=None,
    use_cache: bool = True,
) -> OCRResult:
    data = base64.b64decode(screenshot_b64, validate=True)
    with Image.open(BytesIO(data)) as image:
        image.load()
        size = image.size
    snapshot = snapshot or snapshot_for_image(ctx, data)
    key = ocr_cache_key(ctx, data, transform=transform)
    engine, storage = _storage(ctx)
    image_hash = hashlib.sha256(data).hexdigest()
    if use_cache and storage:
        record = storage.get_image(image_hash)
        entries = (record.extra_metadata or {}).get("ocr_cache", {}) if record else {}
        cached = entries.get(key) if isinstance(entries, dict) else None
        if isinstance(cached, dict) and cached.get("status") in {"complete", "empty"}:
            # Validate cached content too; old unversioned ocr_result is never read.
            try:
                result = OCRResult.model_validate(
                    {**cached, "snapshot": snapshot, "cache_hit": True}
                )
                if result.image_size == size:
                    return result
            except ValueError:
                pass
    inflight = getattr(ctx, "_ocr_inflight", None)
    if not isinstance(inflight, dict):
        inflight = {}
        ctx._ocr_inflight = inflight
    entry = inflight.get(key)
    if not isinstance(entry, _InFlightOCR) or entry.waiters == 0:
        entry = _InFlightOCR(
            asyncio.create_task(
                _recognize(ctx, data, size, snapshot, key, transform, llm, engine, use_cache)
            )
        )
        inflight[key] = entry
    entry.waiters += 1
    try:
        # A cancelled consumer does not cancel the request another consumer owns.
        result = await asyncio.shield(entry.task)
        return result.model_copy(update={"snapshot": snapshot})
    finally:
        entry.waiters -= 1
        if entry.waiters == 0:
            if not entry.task.done():
                entry.task.cancel()
            # The last owner joins cleanup and retrieves terminal exceptions,
            # rather than leaving an unobserved shielded model request behind.
            with suppress(asyncio.CancelledError, Exception):
                await entry.task
            if inflight.get(key) is entry:
                del inflight[key]


async def _recognize(ctx, data, size, snapshot, key, transform, llm, engine, use_cache):
    llm = llm or get_llm(ctx, "default")
    regions = []
    partial = False
    with Image.open(BytesIO(data)) as image:
        tiles = tile_bounds(*size)
        # Leave two calls for format repair within the canonical eight-attempt
        # budget. Very long images are explicitly partial, never falsely complete.
        partial = len(tiles) > 6
        with logical_call_scope():
            for bounds in tiles[:6]:
                tile = image.crop(bounds)
                stream = BytesIO()
                tile.save(stream, format="PNG")
                output = await complete_visual(llm, stream.getvalue(), OCR_PROMPT, OCRModelOutput)
                partial |= output.status == "partial"
                inverse = ImageTransform(offset_x=bounds[0], offset_y=bounds[1])
                for region in output.regions:
                    regions.append(
                        region.model_copy(
                            update={"bbox": inverse.map_bbox(region.bbox, tile.size, size)}
                        )
                    )
    regions = _merge(regions)
    partial |= any(r.uncertain for r in regions)
    status = "partial" if partial else "complete" if regions else "empty"
    result = OCRResult(
        status=status,
        regions=regions,
        snapshot=snapshot,
        image_size=size,
        cache_key=key,
        transform=transform or ImageTransform(),
    )
    if engine and use_cache and status in {"complete", "empty"}:
        # Writes go through the existing engine/storage owner. No partial or
        # exception is persisted as a confirmed empty result.
        engine.get_or_create_image(data)
        engine.merge_image_metadata(
            hashlib.sha256(data).hexdigest(),
            {"ocr_cache": {key: result.model_dump(mode="json", exclude={"snapshot", "cache_hit"})}},
        )
    return result
