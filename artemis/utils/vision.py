"""Image geometry and capture provenance shared by recognition and action gates.

A capture id identifies an observation; the state version identifies the device
state against which a decision was made. Capturing a verification image does not
advance the state version. Action dispatch owns invalidation.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
import math
from typing import Any
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, ConfigDict

from artemis.utils.coordinates import denormalize_bbox, normalize_bbox

COORDINATE_VERSION = "xy-ltrb-1000-v1"
PROVENANCE_KEYS = (
    "snapshot",
    "snapshot_id",
    "source",
    "status",
    "bbox",
    "transform",
    "coordinate_space",
    "coordinate_version",
    "evidence",
    "ocr_status",
    "ocr_version",
    "uncertain",
    "recognition_source",
    "clickable",
    "enabled",
    "visible",
    "actionable_bounds",
    "actionable",
)


def uses_minimax(ctx: Any, role: str = "default", *, is_utils: bool = False) -> bool:
    """Resolve real configurations, while retaining legacy injected test contexts."""
    from artemis.config.llm import LLMConfig
    from artemis.context import ArtemisContext
    from artemis.llm.router import ModelEndpoint
    from artemis.services.llm import context_uses_minimax

    pinned = getattr(ctx, "_llm_endpoint_snapshot", None)
    if isinstance(pinned, dict):
        endpoint = pinned.get((role, is_utils, False))
        if isinstance(endpoint, ModelEndpoint):
            return endpoint.vendor == "minimax"
    if isinstance(getattr(ctx, "llm_config", None), LLMConfig) or issubclass(
        type(ctx), ArtemisContext
    ):
        # Canonical preflight is strict for MiniMax, but does not require an
        # unused legacy text key just to choose the independent OCR backend.
        return context_uses_minimax(ctx)
    return False


class SnapshotRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str
    image_hash: str
    device_id: str | None
    display_id: str = "0"
    width: int
    height: int
    rotation: int | None = None
    state_version: int = 0
    capture_valid: bool = True
    invalid_reason: str | None = None
    coordinate_version: str = COORDINATE_VERSION


def image_size(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        image.load()
        return image.size


def _attr(source: Any, name: str, default=None):
    value = (
        source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)
    )
    # Mock attributes must not become device identifiers or state versions.
    return value if isinstance(value, (str, int, bool, float)) or value is None else default


def state_version(ctx: Any) -> int:
    value = getattr(ctx, "_screen_state_version", 0)
    return value if type(value) is int else 0


def invalidate_snapshot(ctx: Any) -> int:
    """Called by the action owner after a possible screen-changing dispatch."""
    version = state_version(ctx) + 1
    ctx._screen_state_version = version
    ctx._current_snapshot = None
    return version


def _snapshot_origin(metadata) -> tuple[bool, SnapshotRef | None]:
    if isinstance(metadata, SnapshotRef):
        return True, metadata
    if isinstance(metadata, dict) and any(
        key in metadata for key in ("capture_id", "state_version", "image_hash")
    ):
        try:
            return True, SnapshotRef.model_validate(metadata)
        except ValueError:
            return True, None
    nested = getattr(metadata, "snapshot", None) if metadata is not None else None
    if isinstance(nested, (SnapshotRef, dict)):
        return _snapshot_origin(nested)
    return False, None


def create_snapshot(
    ctx: Any, image_bytes: bytes, metadata=None, *, bind: bool = True
) -> SnapshotRef:
    """Describe a fresh capture; existing origin refs never become new captures."""
    has_origin, _ = _snapshot_origin(metadata)
    if has_origin:
        return snapshot_for_image(ctx, image_bytes, metadata)
    reason = _attr(metadata, "invalid_reason")
    valid = _attr(metadata, "capture_valid", True) is True
    try:
        width, height = image_size(image_bytes)
        if width <= 1 or height <= 1:
            valid, reason = False, reason or "synthetic_or_empty_capture"
    except (ValueError, OSError):
        width = height = 0
        valid, reason = False, "undecodable_capture"
    device = getattr(ctx, "device", None)
    raw_device_id = (
        metadata.get("device_id")
        if isinstance(metadata, dict)
        else getattr(metadata, "device_id", None)
    )
    if raw_device_id is None:
        raw_device_id = getattr(device, "device_id", None)
    device_id = raw_device_id if isinstance(raw_device_id, str) and raw_device_id.strip() else None
    if device_id is None:
        valid, reason = False, reason or "unknown_device_id"
    snapshot = SnapshotRef(
        capture_id=uuid4().hex,
        image_hash=hashlib.sha256(image_bytes).hexdigest(),
        device_id=device_id,
        display_id=str(_attr(metadata, "display_id", _attr(device, "display_id", "0"))),
        width=width,
        height=height,
        rotation=_attr(metadata, "rotation", _attr(device, "rotation")),
        state_version=state_version(ctx),
        capture_valid=valid,
        invalid_reason=reason,
    )
    if bind and ctx is not None:
        ctx._current_snapshot = snapshot
    return snapshot


def snapshot_for_image(ctx: Any, image_bytes: bytes, metadata=None) -> SnapshotRef:
    """Reuse a decision capture when reopening its image, without rebinding history.

    Explicit origin identity is required, even for pixel-identical images.
    Unknown saved/history bytes cannot acquire the current capture id by hash.
    Crops pass their root snapshot and ImageTransform directly to recognition.
    """
    current = getattr(ctx, "_current_snapshot", None)
    has_origin, origin = _snapshot_origin(metadata)
    measured = create_snapshot(ctx, image_bytes, bind=False)
    if origin is None:
        return measured.model_copy(
            update={
                "capture_valid": False,
                "invalid_reason": "invalid_snapshot_origin" if has_origin else "unbound_image",
            }
        )

    def invalid(reason):
        # Preserve the old id/version/hash in traces, instead of laundering them
        # into a newly observed decision whose pixels happen to be identical.
        return origin.model_copy(update={"capture_valid": False, "invalid_reason": reason})

    if not origin.capture_valid or not measured.capture_valid:
        return invalid(origin.invalid_reason or measured.invalid_reason or "invalid_capture")
    if any(
        getattr(origin, key) != getattr(measured, key) for key in ("image_hash", "width", "height")
    ):
        return invalid("snapshot_image_mismatch")
    device = getattr(ctx, "device", None)
    for key in ("device_id", "display_id", "rotation"):
        expected = _attr(device, key)
        if expected is not None and str(expected) != str(getattr(origin, key)):
            return invalid("snapshot_device_mismatch")
    if origin.state_version != state_version(ctx):
        return invalid("stale_snapshot")
    if not isinstance(current, SnapshotRef):
        return invalid("unbound_snapshot_origin")
    if any(getattr(origin, key) != getattr(current, key) for key in SnapshotRef.model_fields):
        return invalid("snapshot_capture_mismatch")
    return current


class ImageTransform(BaseModel):
    """Inverse of crop/resize: root_pixel = input_pixel / scale + offset."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    offset_x: float = 0.0
    offset_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0

    def to_root(self, x: float, y: float) -> tuple[float, float]:
        if (
            not all(
                math.isfinite(v)
                for v in (x, y, self.offset_x, self.offset_y, self.scale_x, self.scale_y)
            )
            or min(self.scale_x, self.scale_y) <= 0
        ):
            raise ValueError("Invalid image transform")
        return x / self.scale_x + self.offset_x, y / self.scale_y + self.offset_y

    def compose(self, child: ImageTransform) -> ImageTransform:
        x, y = self.to_root(child.offset_x, child.offset_y)
        return ImageTransform(
            offset_x=x,
            offset_y=y,
            scale_x=self.scale_x * child.scale_x,
            scale_y=self.scale_y * child.scale_y,
        )

    def map_bbox(
        self, bbox, input_size: tuple[int, int], root_size: tuple[int, int]
    ) -> list[float]:
        left, t, r, b = denormalize_bbox(bbox, *input_size)
        left, t = self.to_root(left, t)
        r, b = self.to_root(r, b)
        return normalize_bbox([left, t, r, b], *root_size)


def provenance(
    snapshot: SnapshotRef | None,
    *,
    source: str,
    bbox=None,
    transform=None,
    evidence: str = "unverified",
    status: str = "found",
) -> dict:
    return {
        "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "snapshot_id": snapshot.capture_id if snapshot else None,
        "source": source,
        "bbox": bbox,
        "transform": (transform or ImageTransform()).model_dump(),
        "coordinate_space": "normalized",
        "coordinate_version": COORDINATE_VERSION,
        "evidence": evidence,
        "status": status,
    }


def copy_provenance(value: dict) -> dict:
    return {key: value[key] for key in PROVENANCE_KEYS if key in value}


def candidate_from_bbox(
    label: str, bbox, snapshot: SnapshotRef, *, source="minimax", transform=None, status="found"
) -> dict:
    left, t, r, b = bbox
    point = [(left + r) / 2, (t + b) / 2]
    return {
        "label": label,
        "point": point,
        "coords": point,
        "bounds": bbox,
        **provenance(snapshot, source=source, bbox=bbox, transform=transform, status=status),
    }


def stamp_elements(elements: list[dict], snapshot: SnapshotRef) -> list[dict]:
    """Attach capture identity to the existing formatted XML/OCR element list."""
    for element in elements:
        bounds = element.get("bounds")
        try:
            bbox = normalize_bbox(bounds, snapshot.width, snapshot.height) if bounds else None
        except ValueError:
            # ui_filter preserves semantic off-screen rows as clipped points.
            # Keep their indexes/text, but never promote them to actionable boxes.
            bbox = None
        inherited = copy_provenance(element)
        element.update(
            provenance(
                snapshot,
                source=element.get("source") or ("ocr" if element.get("is_ocr") else "xml"),
                bbox=bbox,
            )
        )
        element.update(inherited)
        # Even cached OCR belongs to this observation, never its old capture.
        element["snapshot"] = snapshot.model_dump(mode="json")
        element["snapshot_id"] = snapshot.capture_id
        element["bbox"] = bbox
        element["coordinate_space"] = "normalized"
        element["coordinate_version"] = COORDINATE_VERSION
        if (
            bbox is None
            or str(element.get("visible", "true")).lower() == "false"
            or not snapshot.capture_valid
        ):
            element["evidence"] = "unverified"
            element["actionable_bounds"] = None
            element["actionable"] = False
            if (bbox is None and bounds is not None) or str(
                element.get("visible", "true")
            ).lower() == "false":
                element["visible"] = False
            continue
        if bounds:
            left, top, right, bottom = bounds
            visible = [
                max(0, left),
                max(0, top),
                min(snapshot.width, right),
                min(snapshot.height, bottom),
            ]
            if visible[2] > visible[0] and visible[3] > visible[1]:
                element["actionable_bounds"] = visible
                element["center"] = [
                    min(snapshot.width - 1, round((visible[0] + visible[2]) / 2)),
                    min(snapshot.height - 1, round((visible[1] + visible[3]) / 2)),
                ]
                if (
                    snapshot.capture_valid
                    and element["source"] == "xml"
                    and (
                        element.get("clickable") is True
                        or str(element.get("clickable")).lower() == "true"
                    )
                    and str(element.get("enabled", "true")).lower() != "false"
                    and str(element.get("visible", "true")).lower() != "false"
                ):
                    element["evidence"] = "xml_actionable"
                    element["actionable"] = True
    return elements
