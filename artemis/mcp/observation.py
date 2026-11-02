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

"""Screen observation: capture, fuse, index, and persist -- without LangGraph State.

This is the act-then-observe half that used to be welded into every executor action
(``capture_screenshot_and_parse_ui``). Split out so that actuation and observation are
separate calls: the Flash adapter observes after each action, while the
Validator -- which runs its own UI-change polling -- simply never calls it.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import time

from artemis.config import get_temp_dir
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionCode, ObserveResult
from artemis.utils.vision import SnapshotRef, create_snapshot, stamp_elements, uses_minimax
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml, pure_xml_hierarchy
from artemis.utils.visualization import format_minimal_list_with_elements
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["observe", "apply_observation_to_state"]


def apply_observation_to_state(
    state, observation: ObserveResult, screenshot_bytes: bytes | None = None
) -> None:
    """Write the same capture identity and raw XML through Flash/Pro adapters."""
    if not observation.ok:
        return
    if observation.hierarchy_ok:
        state.indexed_points = [element["center"] for element in observation.elements]
        state.indexed_elements = observation.elements
    if observation.screenshot_path:
        state.latest_screenshot = observation.screenshot_path
    snapshot = getattr(observation, "snapshot", None)
    if not isinstance(snapshot, SnapshotRef):
        return
    previous = getattr(state, "operator_raw_data", None)
    raw = dict(previous) if isinstance(previous, dict) else {}
    xml = getattr(observation, "xml_hierarchy", None)
    xml = copy.deepcopy(xml) if isinstance(xml, list) and observation.hierarchy_ok else None
    raw.update(
        snapshot=snapshot.model_dump(mode="json"),
        xml_hierarchy=xml,
        width=snapshot.width,
        height=snapshot.height,
        hierarchy_ok=observation.hierarchy_ok,
        ocr_results=copy.deepcopy(observation.ocr_results),
        ocr_outcome=dict(observation.ocr_outcome),
    )
    if screenshot_bytes:
        raw["screenshot_b64"] = base64.b64encode(screenshot_bytes).decode("ascii")
    else:
        raw.pop("screenshot_b64", None)
    state.operator_raw_data = raw
    state.latest_ui_hierarchy = copy.deepcopy(xml)


async def observe(
    ctx: ArtemisContext,
    controller: UnifiedMobileController,
    settle_ms: int = 400,
    ocr_results: list | None = None,
) -> tuple[ObserveResult, bytes | None]:
    """Captures and indexes the current screen.

    Returns the structured observation plus the raw screenshot bytes (an in-process
    convenience; the serialized ``ObserveResult`` carries only the persisted path).

    Mirrors the historical ``capture_screenshot_and_parse_ui`` behavior exactly:
    a settling delay, screen capture, XML fusion and element indexing, persistence via
    the data engine with a temp-file fallback -- and the same failure semantics: a
    failed capture yields ``ok=False``; a failed hierarchy parse still returns the
    screenshot with ``hierarchy_ok=False``.
    """
    try:
        if settle_ms > 0:
            logger.info(f"Delaying {settle_ms / 1000:.1f}s before capturing screen state...")
            await asyncio.sleep(settle_ms / 1000.0)
        else:
            logger.info("Retrieving screen data directly (skip settling)...")
        device_data = await controller.get_screen_data()
        screenshot_bytes = base64.b64decode(device_data.base64)
        snapshot = getattr(device_data, "snapshot", None)
        if not isinstance(snapshot, SnapshotRef):
            snapshot = create_snapshot(ctx, screenshot_bytes, device_data, bind=False)
        if uses_minimax(ctx) and not snapshot.capture_valid:
            raise ValueError(f"Invalid capture: {snapshot.invalid_reason}")
        ctx._current_snapshot = snapshot
    except Exception as e:
        logger.error(f"Failed to capture screen state: {e}")
        return (
            ObserveResult(
                ok=False,
                code=ActionCode.DEVICE_ERROR,
                message=f"Failed to capture screen state: {e}",
                hierarchy_ok=False,
            ),
            None,
        )

    xml_hierarchy = None
    ocr_results = ocr_results or []
    ocr_status = {}
    elements: list[dict] = []
    hierarchy_ok = True
    width = height = None
    try:
        xml_hierarchy = device_data.elements
        if uses_minimax(ctx):
            xml_hierarchy = pure_xml_hierarchy(xml_hierarchy)
            ocr_results = [
                row
                for row in ocr_results
                if row.get("source") == "minimax"
                and row.get("ocr_version") == "regions-ltrb-v1"
                and row.get("snapshot_id") == snapshot.capture_id
            ]
            ocr_status = (
                {
                    "status": "partial"
                    if any(row.get("ocr_status") == "partial" for row in ocr_results)
                    else "complete",
                    "source": "minimax",
                    "snapshot_id": snapshot.capture_id,
                }
                if ocr_results
                else {
                    "status": "disabled",
                    "reason": "ocr_not_requested_for_observation",
                    "source": "minimax",
                    "snapshot_id": snapshot.capture_id,
                }
            )
        width = snapshot.width or device_data.width or 1080
        height = snapshot.height or device_data.height or 2400
        if ctx.device:
            ctx.device.device_width = width
            ctx.device.device_height = height

        fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)
        minimal_list_str, elements, _ = format_minimal_list_with_elements(fused_xml, width, height)
        if snapshot.width > 0 and snapshot.height > 0:
            stamp_elements(elements, snapshot)
    except Exception as e:
        logger.error(f"Failed to fetch or fuse UI hierarchy: {e}")
        minimal_list_str = "Not available due to hierarchy error."
        hierarchy_ok = False

    screenshot_path = None
    if ctx.data_engine:
        try:
            image_name = ctx.data_engine.get_or_create_image(
                screenshot_bytes,
                ui_tree=xml_hierarchy,
                ocr_result=ocr_results,
            )
            screenshot_path = str(ctx.data_engine.get_image_path(image_name))
        except Exception as e:
            logger.error(f"Failed to save image in DataEngine: {e}")

    if not screenshot_path:
        try:
            temp_dir = get_temp_dir("screenshots")
            temp_file = temp_dir / f"screenshot_{int(time.time())}.jpg"
            temp_file.write_bytes(screenshot_bytes)
            screenshot_path = str(temp_file)
        except Exception as e:
            logger.error(f"Failed to save fallback screenshot: {e}")

    from pathlib import Path

    return (
        ObserveResult(
            ok=True,
            message="Screen observed.",
            snapshot=snapshot,
            screenshot_path=screenshot_path,
            image_name=Path(screenshot_path).stem if screenshot_path else None,
            width=width,
            height=height,
            elements_text=minimal_list_str,
            elements=elements,
            xml_hierarchy=xml_hierarchy if hierarchy_ok else None,
            ocr_results=ocr_results,
            ocr_outcome=ocr_status,
            hierarchy_ok=hierarchy_ok,
        ),
        screenshot_bytes,
    )
