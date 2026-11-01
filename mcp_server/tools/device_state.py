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

"""MCP Tool: mobile_get_device_state."""

from __future__ import annotations

import base64
import os

from mcp_server.base import mcp
from mcp_server.utils import env_utils


@mcp.tool()
async def mobile_get_device_state(view_type: str, device_serial: str | None = None) -> str:
    """Real-time mobile device state observer (for debugging and validation).

    Retrieves a real-time screenshot or a simplified UI element tree from the
    target device — useful for inspecting device status or tracking a
    subagent's progress.

    Args:
        view_type: Observation type:
          - "screenshot": captures the screen, saves it to the workspace, and
            returns the image's local file URI.
          - "hierarchy": returns the simplified text-labeled element list —
            exactly what the automation subagent sees when making decisions.
        device_serial: Optional device serial (e.g. "emulator-5554") to inspect
          a specific device; omitted → the default connected device. With
          several devices attached, confirm the target with the user
          (`adb devices -l` lists serials).
    """

    env_utils.import_artemis_package()
    from artemis.mcp.adb_server import _get_controller
    from artemis.utils.ocr_xml_fusion import (
        fuse_ocr_with_xml,
        pure_xml_hierarchy,
        _detect_status_bar_height,
        _crop_image_remove_status_bar,
        _map_coordinates_back,
    )
    from artemis.utils.ocr_api import is_ocr_configured, perform_ocr, ocr_outcome, ocr_status_text
    from artemis.utils.vision import SnapshotRef, ImageTransform, create_snapshot, uses_minimax
    from artemis.utils.visualization import format_minimal_list_with_elements

    try:
        controller = _get_controller(device_serial=device_serial)
        device_width = controller.ctx.device.device_width
        device_height = controller.ctx.device.device_height
    except Exception as e:
        return f"Error: Failed to initialize/lock Android device controller: {e}"

    try:
        device_data = await controller.get_screen_data()
        latest_screenshot_b64 = device_data.base64
        xml_hierarchy = device_data.elements
        if uses_minimax(controller.ctx):
            xml_hierarchy = pure_xml_hierarchy(xml_hierarchy)
        snapshot = getattr(device_data, "snapshot", None)
        if not isinstance(snapshot, SnapshotRef):
            snapshot = create_snapshot(
                controller.ctx, base64.b64decode(latest_screenshot_b64), device_data, bind=False
            )
        device_width = snapshot.width or device_data.width
        device_height = snapshot.height or device_data.height

        project_root = env_utils.get_project_root()

        if view_type == "screenshot":
            screenshot_bytes = base64.b64decode(latest_screenshot_b64)
            device_id = controller.ctx.device.device_id
            safe_device_id = "".join(
                [c if c.isalnum() or c in ("-", "_") else "_" for c in device_id]
            )
            screenshot_filename = f"live_screenshot_{safe_device_id}.jpg"
            screenshot_path = os.path.join(project_root, screenshot_filename)

            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)

            return f"file://{screenshot_path}"

        elif view_type == "hierarchy":
            ocr_results = []
            request_outcome = {
                "status": "disabled",
                "reason": "ocr_unconfigured",
                "source": "minimax",
                "snapshot_id": snapshot.capture_id,
            }
            if is_ocr_configured(controller.ctx):
                try:
                    screen_height = device_height
                    status_bar_height = _detect_status_bar_height(xml_hierarchy, screen_height)
                    if status_bar_height > 0:
                        cropped_b64, _, _ = _crop_image_remove_status_bar(
                            latest_screenshot_b64, status_bar_height
                        )
                        applied_offset = (
                            status_bar_height if cropped_b64 != latest_screenshot_b64 else 0
                        )
                        raw_ocr_results = await perform_ocr(
                            cropped_b64,
                            ctx=controller.ctx,
                            snapshot=snapshot,
                            transform=ImageTransform(offset_y=applied_offset),
                        )
                        ocr_results = (
                            _map_coordinates_back(raw_ocr_results, applied_offset)
                            if applied_offset
                            else raw_ocr_results
                        )
                    else:
                        ocr_results = await perform_ocr(
                            latest_screenshot_b64, ctx=controller.ctx, snapshot=snapshot
                        )
                    request_outcome = ocr_outcome(controller.ctx)
                except Exception as error:
                    request_outcome = {
                        "status": "error",
                        "reason": type(error).__name__,
                        "source": "minimax",
                        "snapshot_id": snapshot.capture_id,
                    }
                    controller.ctx._ocr_last_outcome = dict(request_outcome)
                    ocr_results = []

            fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)
            hierarchy_text, elements, labels = format_minimal_list_with_elements(
                fused_xml, width=device_width, height=device_height
            )
            status_text = ocr_status_text(controller.ctx, request_outcome)
            if status_text:
                hierarchy_text += "\n" + status_text
            return hierarchy_text

        else:
            return f"Error: Invalid view_type '{view_type}'. Supported types are 'screenshot' and 'hierarchy'."

    except Exception as e:
        return f"Error: An unexpected error occurred while communicating with the device: {e}"
