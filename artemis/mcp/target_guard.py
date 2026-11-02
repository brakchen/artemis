"""MiniMax Agent target checks shared by Pro and Flash, outside the wire schema.

Only adapters supply capture identity. Model arguments cannot grant provenance or
XML evidence. Unknown is recoverable by observing again, never permission to act.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
from enum import Enum
import hashlib
import math
from typing import Any
from types import SimpleNamespace

from artemis.utils.coordinates import (
    denormalize_point,
    normalize_bbox,
    validate_normalized_point,
    validate_bbox,
)
from artemis.utils.vision import (
    COORDINATE_VERSION,
    SnapshotRef,
    ImageTransform,
    state_version,
    image_size,
)


class TargetDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TargetVerdict:
    decision: TargetDecision
    reason: str = ""
    pixel_review_allowed: bool = False
    identity: tuple[tuple[str, str], ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is TargetDecision.ALLOW


def snapshot_error(ctx: Any, expected: SnapshotRef | dict | None) -> str | None:
    """Cheap final check; safe in ActionSession's owner (must never queue I/O)."""
    try:
        if isinstance(expected, dict):
            if set(SnapshotRef.model_fields) - expected.keys():
                return "Incomplete snapshot metadata; observe again."
            expected = SnapshotRef.model_validate(expected, strict=True)
        current = getattr(ctx, "_current_snapshot", None)
        if not isinstance(expected, SnapshotRef) or not expected.capture_valid:
            return "Missing or invalid target snapshot; observe again."
        if not isinstance(current, SnapshotRef) or expected != current:
            return "Target belongs to another observation; observe again."
        if expected.state_version != state_version(ctx):
            return "Target snapshot is stale; observe again."
        if expected.coordinate_version != COORDINATE_VERSION:
            return "Unknown coordinate contract; observe again."
        device = getattr(ctx, "device", None)
        for key, field in (
            ("device_id", "device_id"),
            ("display_id", "display_id"),
            ("rotation", "rotation"),
            ("device_width", "width"),
            ("device_height", "height"),
        ):
            actual = getattr(device, key, None)
            if key in ("device_id", "device_width", "device_height") and (
                actual is None or isinstance(actual, bool)
            ):
                return "Actual device identity/dimensions are unavailable; observe again."
            if actual is not None and str(actual) != str(getattr(expected, field)):
                return "Device/display/rotation/dimensions changed; observe again."
        if min(expected.width, expected.height) <= 1:
            return "Invalid capture dimensions; observe again."
        if type(expected.rotation) is not int:
            return "Capture rotation is unknown; observe again."
        active_raw = getattr(ctx, "_raw_adb_active", 0)
        if isinstance(active_raw, int) and active_raw > 0:
            return "Raw ADB command may still be active; wait for it or terminate it, then observe again."
        # A raw shell task may mutate the screen after the command has returned.
        registry = getattr(ctx, "adb_task_registry", None)
        background = getattr(registry, "background", None)
        if isinstance(background, dict) and background:
            return "Raw ADB task is still active; finish it and observe again."
    except (ValueError, TypeError, AttributeError):
        return "Invalid snapshot metadata; observe again."
    return None


def changes_screen(name: str) -> bool:
    """Unknown backend extensions are conservatively effectful."""
    return name not in {
        "observe_screen",
        "take_screenshot",
        "get_ui_hierarchy",
        "wait_for_delay",
        "wait_for_text",
        "get_device_state",
    }


def decision_snapshot(state: Any) -> dict | None:
    raw = getattr(state, "operator_raw_data", None)
    return raw.get("snapshot") if isinstance(raw, dict) else None


def canonical_target_action(ctx, action: dict) -> dict:
    """Describe exactly the normalized point the existing wire adapter sends."""
    if action.get("coordinate_space") == "normalized":
        point = validate_normalized_point(action.get("coordinates"))
    else:
        from artemis.mcp.action_names import to_canonical_call

        snapshot = ctx._current_snapshot
        _, wire = to_canonical_call(action, (snapshot.width, snapshot.height), strict=True)
        point = validate_normalized_point(wire.get("target") or wire.get("start"))
    return {**action, "coordinates": point, "coordinate_space": "normalized"}


async def verification_capture(ctx, session, expected) -> str:
    """Require live physical capture identity, including rotation and display."""
    error = snapshot_error(ctx, expected)
    if error:
        raise ValueError(error)
    image, metadata = await session.verification_capture(timeout=3.0)
    if not isinstance(metadata, dict) or set(SnapshotRef.model_fields) - metadata.keys():
        raise ValueError("Missing live capture metadata; observe again")
    live = SnapshotRef.model_validate(metadata, strict=True)
    original = SnapshotRef.model_validate(expected)
    if not live.capture_valid or any(
        getattr(live, key) != getattr(original, key)
        for key in (
            "device_id",
            "display_id",
            "width",
            "height",
            "rotation",
            "state_version",
            "coordinate_version",
        )
    ):
        raise ValueError("Live device/display/rotation/dimensions mismatch; observe again")
    data = base64.b64decode(image, validate=True)
    if (
        image_size(data) != (live.width, live.height)
        or hashlib.sha256(data).hexdigest() != live.image_hash
    ):
        raise ValueError("Live capture bytes do not match capture metadata; observe again")
    return image


def _truth(value) -> bool:
    return value is True or value == "true"


def _box(value) -> list[float] | None:
    if isinstance(value, str):
        from artemis.utils.visualization import parse_bounds

        value = parse_bounds(value)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in value
    ):
        return None
    left, top, right, bottom = value
    return list(value) if right > left and bottom > top else None


def _nodes(tree, ancestors=()):
    for node in tree or []:
        if not isinstance(node, dict):
            continue
        yield node, ancestors
        yield from _nodes(node.get("children"), (*ancestors, node))


def _contains(box, point):
    return box and box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _xml_identity(node: dict) -> dict[str, str]:
    fields = {
        "resource_id": node.get("resource_id") or node.get("resource-id"),
        "text": node.get("text"),
        "content_desc": node.get("content-desc")
        or node.get("content_desc")
        or node.get("accessibilityText"),
        "class": node.get("class"),
    }
    return {
        key: value.strip()
        for key, value in fields.items()
        if isinstance(value, str) and value.strip()
    }


def local_target_verdict(
    ctx: Any, action: dict, state: Any, *, expected_identity: tuple[tuple[str, str], ...] = ()
) -> TargetVerdict:
    """Allow only actual actionable XML, never a model-supplied evidence label."""
    error = snapshot_error(ctx, action.get("snapshot"))
    if error:
        return TargetVerdict(TargetDecision.BLOCK, error)
    indexed = any(
        action.get(k)
        for k in ("target_text", "target_bounds", "target_resource_id", "target_class", "source")
    )
    if indexed:
        required = {"source", "snapshot_id", "bbox", "transform", "status", "coordinate_version"}
        if (
            required - action.keys()
            or action.get("snapshot_id") != ctx._current_snapshot.capture_id
            or action.get("coordinate_version") != COORDINATE_VERSION
        ):
            return TargetVerdict(
                TargetDecision.BLOCK, "Incomplete target provenance; observe again."
            )
        try:
            if (
                not isinstance(action["transform"], dict)
                or set(ImageTransform.model_fields) - action["transform"].keys()
            ):
                raise ValueError("Incomplete target transform")
            transform = ImageTransform.model_validate(action["transform"])
            transform.to_root(0, 0)
        except (ValueError, TypeError):
            return TargetVerdict(TargetDecision.BLOCK, "Invalid target transform; observe again.")
    elif (
        not isinstance(action.get("target_description"), str)
        or not action["target_description"].strip()
    ):
        return TargetVerdict(TargetDecision.BLOCK, "Target description is missing.")
    if action.get("visible") is False or action.get("visible") == "false":
        return TargetVerdict(TargetDecision.BLOCK, "Target is not visible; observe again.")
    if action.get("source") and not _box(action.get("bbox")):
        return TargetVerdict(TargetDecision.BLOCK, "Target has no visible bounds; observe again.")
    is_ocr = action.get("ocr_version") is not None or "[OCR]" in str(
        action.get("target_class") or ""
    )
    certain_ocr = (
        is_ocr
        and action.get("ocr_version") == "regions-ltrb-v1"
        and action.get("uncertain") is False
        and action.get("ocr_status") in {"partial", "complete"}
    )
    valid_status = action.get("status", "found") == "found" or (
        certain_ocr and action.get("status") in {"partial", "complete"}
    )
    if action.get("uncertain") is True or not valid_status or (is_ocr and not certain_ocr):
        return TargetVerdict(TargetDecision.UNKNOWN, "Target recognition is uncertain.")
    current = ctx._current_snapshot
    try:
        prepared = canonical_target_action(ctx, action)
        point = denormalize_point(*prepared["coordinates"], current.width, current.height)
        if not (0 <= point[0] < current.width and 0 <= point[1] < current.height):
            raise ValueError("Point outside capture")
        if action.get("bbox") and not _contains(
            validate_bbox(action["bbox"]),
            [point[0] * 1000 / current.width, point[1] * 1000 / current.height],
        ):
            return TargetVerdict(
                TargetDecision.BLOCK,
                "Canonical dispatch point misses target bounds; observe again.",
            )
    except (ValueError, TypeError):
        return TargetVerdict(TargetDecision.BLOCK, "Invalid target coordinates.")
    raw = getattr(state, "operator_raw_data", None)
    if not isinstance(raw, dict) or raw.get("snapshot") != current.model_dump(mode="json"):
        return TargetVerdict(TargetDecision.BLOCK, "Decision observation metadata is missing.")
    if action.get("source") != "xml":
        return TargetVerdict(TargetDecision.UNKNOWN, "Target needs pixel validation.", True)
    bbox = action.get("bbox")
    nodes = list(_nodes(raw.get("xml_hierarchy")))
    matches = []
    identity_conflict = False
    for node, ancestors in nodes:
        bounds = _box(node.get("bounds"))
        try:
            same_box = bounds and normalize_bbox(bounds, current.width, current.height) == bbox
        except ValueError:
            same_box = False
        actual = _xml_identity(node)
        if expected_identity:
            constraints = [(value, actual.get(key)) for key, value in expected_identity]
        else:
            # The formatted label may originate from text or content-desc.
            # Both observed id and label must agree when both exist.
            constraints = [
                (action.get("target_resource_id"), actual.get("resource_id")),
                (action.get("target_text"), actual.get("text") or actual.get("content_desc")),
            ]
            constraints = [
                (expected.strip(), value)
                for expected, value in constraints
                if isinstance(expected, str) and expected.strip()
            ]
        identity = bool(constraints) and all(
            expected == actual_value for expected, actual_value in constraints
        )
        shared_anchor = bool(expected_identity) and any(
            key in {"resource_id", "text", "content_desc"} and actual.get(key) == value
            for key, value in expected_identity
        )
        if (same_box or shared_anchor) and constraints and not identity:
            identity_conflict = True
        if same_box and identity:
            matches.append((node, ancestors))
    if len(matches) != 1:
        if expected_identity:
            return TargetVerdict(
                TargetDecision.UNKNOWN,
                "Observed XML target has no unique live match; observe again.",
            )
        if identity_conflict:
            return TargetVerdict(
                TargetDecision.UNKNOWN, "XML target identity changed; observe again."
            )
        return TargetVerdict(
            TargetDecision.UNKNOWN, "No unique XML target with identity and bounds.", True
        )
    node, ancestors = matches[0]
    identity = tuple(sorted(_xml_identity(node).items()))
    if any(
        n.get("visible") in (False, "false") or n.get("enabled") in (False, "false")
        for n in (node, *ancestors)
    ):
        return TargetVerdict(TargetDecision.BLOCK, "XML target is hidden or disabled.")
    if not _contains(_box(node.get("bounds")), point):
        return TargetVerdict(TargetDecision.BLOCK, "Point misses target's visible intersection.")
    owner = next(
        (
            n
            for n in (node, *reversed(ancestors))
            if _truth(n.get("clickable"))
            or (action.get("action") == "swipe" and _truth(n.get("scrollable")))
        ),
        None,
    )
    if owner is None or owner.get("enabled") in (False, "false"):
        return TargetVerdict(
            TargetDecision.UNKNOWN, "XML lacks an actionable node or ancestor.", True, identity
        )
    # Never aim at a broad parent center over another child or unrelated overlay.
    for other, parents in nodes:
        if other is node or any(other is n for n in ancestors):
            continue
        if _contains(_box(other.get("bounds")), point):
            return TargetVerdict(
                TargetDecision.UNKNOWN,
                "Another XML element occupies the target point.",
                True,
                identity,
            )
    return TargetVerdict(TargetDecision.ALLOW, identity=identity)


async def guard_target(ctx, session, action, state, screenshot_b64, *, get_llm_fn) -> TargetVerdict:
    verdict = local_target_verdict(ctx, action, state)
    if verdict.allowed or (verdict.pixel_review_allowed and verdict.identity):
        # Device animation/another actor can change XML without our state version.
        # Reuse the trusted identity against the live tree, with no model call.
        try:
            await verification_capture(ctx, session, action.get("snapshot"))
            live = await session.ui_hierarchy(timeout=3.0)
            raw = dict(state.operator_raw_data)
            raw["xml_hierarchy"] = live
            verdict = local_target_verdict(
                ctx,
                action,
                SimpleNamespace(operator_raw_data=raw),
                expected_identity=verdict.identity,
            )
        except Exception:
            verdict = TargetVerdict(TargetDecision.UNKNOWN, "Live XML unavailable.")
    if verdict.decision is not TargetDecision.UNKNOWN or not verdict.pixel_review_allowed:
        action["precondition_outcome"] = {
            "decision": verdict.decision.value,
            "source": action.get("source") or "local",
        }
        return verdict
    from artemis.agents.validator.precondition_pixel import validate_action_precondition_pixel

    passed, _, reason = await validate_action_precondition_pixel(
        ctx, session, action, screenshot_b64, state=state, get_llm_fn=get_llm_fn
    )
    decision = action.get("precondition_outcome", {}).get("decision", "unknown")
    return TargetVerdict(TargetDecision.ALLOW if passed else TargetDecision(decision), reason)
