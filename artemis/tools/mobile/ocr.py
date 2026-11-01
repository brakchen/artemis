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

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr, ocr_outcome, ocr_status_text
from artemis.utils.coordinates import normalize_point
from artemis.utils.vision import (
    image_size,
    uses_minimax,
    create_snapshot,
    snapshot_for_image,
    copy_provenance,
)
from third_party.mobile_use.utils.logger import get_logger


class OcrArgs(BaseModel):
    """Arguments schema for OCR recognition."""


logger = get_logger(__name__)

OCR_DOCSTRING = (
    "[EXPLORER] Performs OCR on the current screen and returns the detected"
    " text along with their coordinates."
)


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
async def run_ocr_core(
    ctx: ArtemisContext | None = None,
    state: State | None = None,
    driver: BaseDeviceDriver | None = None,
) -> str:
    """Universal OCR core function: reads screenshot from state or driver and returns results."""
    screenshot_b64 = None
    captured = None
    screenshot_path = getattr(state, "latest_screenshot", None) if state else None

    if screenshot_path:
        if not Path(screenshot_path).exists():
            logger.error(f"Screenshot file does not exist: {screenshot_path}")
            raise FileNotFoundError(f"Screenshot file does not exist: {screenshot_path}")

        try:
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
            logger.info(f"OCR loaded screenshot from file: {screenshot_path}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to read screenshot from {screenshot_path}: {e}")
            raise e
    elif driver is not None and hasattr(driver, "get_screen_data"):
        try:
            screen_data = await driver.get_screen_data()
            captured = screen_data
            if hasattr(screen_data, "screenshot_base64") and screen_data.screenshot_base64:
                screenshot_b64 = screen_data.screenshot_base64
            elif hasattr(screen_data, "screenshot_bytes") and screen_data.screenshot_bytes:
                screenshot_b64 = base64.b64encode(screen_data.screenshot_bytes).decode("utf-8")
            elif hasattr(screen_data, "base64") and screen_data.base64:
                screenshot_b64 = screen_data.base64
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to capture screenshot from driver: {e}")
            raise e
    else:
        logger.error("No screenshot path found in state.latest_screenshot and no driver available")
        raise ValueError(
            "No screenshot path found in state.latest_screenshot and no driver available"
        )

    if not screenshot_b64:
        raise ValueError("Failed to obtain screenshot for OCR.")

    data = base64.b64decode(screenshot_b64)
    try:
        width, height = image_size(data)
    except (ValueError, OSError):
        if uses_minimax(ctx):
            raise ValueError("Actual OCR image dimensions are required") from None
        # Legacy injected image readers retain their existing device boundary.
        width = getattr(getattr(ctx, "device", None), "device_width", None)
        height = getattr(getattr(ctx, "device", None), "device_height", None)
        if not (type(width) is int and type(height) is int and width > 0 and height > 0):
            width, height = driver.screen_size if driver else (None, None)
        if not (type(width) is int and type(height) is int and width > 0 and height > 0):
            raise ValueError("Actual OCR image dimensions are required")
    raw = getattr(state, "operator_raw_data", None) if state is not None else None
    metadata = raw.get("snapshot") if isinstance(raw, dict) else None
    snapshot = (
        create_snapshot(ctx, data, captured, bind=False)
        if captured
        else snapshot_for_image(ctx, data, metadata)
    )
    if (
        uses_minimax(ctx)
        and not snapshot.capture_valid
        and snapshot.invalid_reason != "unbound_image"
    ):
        raise ValueError(f"Invalid OCR capture: {snapshot.invalid_reason}")
    results = await perform_ocr(screenshot_b64, ctx=ctx, snapshot=snapshot)
    request_outcome = ocr_outcome(ctx)
    if results:
        normalized_results = []
        for item in results:
            text = item.get("text")
            vertices = item.get("position") or []

            # Calculate center point
            x_coords = [v.get("x", 0) for v in vertices]
            y_coords = [v.get("y", 0) for v in vertices]

            if x_coords and y_coords:
                min_x, max_x = min(x_coords), max(x_coords)
                min_y, max_y = min(y_coords), max(y_coords)

                center_x = (min_x + max_x) / 2
                center_y = (min_y + max_y) / 2

                # Normalize to 0-1000
                norm_x, norm_y = normalize_point(center_x, center_y, width, height)

                normalized_results.append(
                    {"text": text, "coordinates": [norm_x, norm_y], **copy_provenance(item)}
                )
            else:
                normalized_results.append({"text": text, "coordinates": None})

        return json.dumps(normalized_results, ensure_ascii=False, indent=2)
    return ocr_status_text(ctx, request_outcome) or "No text detected on the screen."


class OcrRecognitionTool(ArtemisTool):
    """Universal tool for performing OCR on the current screen."""

    def __init__(self, category: ToolCategory = "explorer"):
        super().__init__(
            name="ocr_recognition",
            description=OCR_DOCSTRING,
            args_schema=OcrArgs,
            category=category,
        )

    def is_available(self, ctx: Any = None) -> bool:
        return is_ocr_configured(ctx)

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        has_failed = False
        output = ""
        error_message = None

        try:
            output = await run_ocr_core(ctx=ctx, state=st, driver=driver)
        except Exception as e:  # pylint: disable=broad-exception-caught
            has_failed = True
            error_message = f"Failed to perform OCR: {str(e)}"

        if st is not None:
            return ToolMessage(
                tool_call_id=tcid or "",
                content=ocr_recognition_wrapper.on_failure_fn(error_message)
                if has_failed
                else ocr_recognition_wrapper.on_success_fn(output),
                additional_kwargs={"error": error_message} if has_failed else {},
                status="error" if has_failed else "success",
            )

        if has_failed:
            return ocr_recognition_wrapper.on_failure_fn(error_message)
        return ocr_recognition_wrapper.on_success_fn(output)


# Universal tool instance & aliases
ocr_recognition = OcrRecognitionTool()
OcrRecognition = OcrRecognitionTool
OCRRecognition = OcrRecognitionTool
OcrTool = OcrRecognitionTool


def get_ocr_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports ocr_recognition as a LangChain BaseTool."""
    return trace_langchain_tool(ocr_recognition.to_langchain_tool(ctx), ctx)


ocr_recognition_wrapper = ToolWrapper(
    tool_fn_getter=get_ocr_tool,
    on_success_fn=lambda output: f"OCR Recognition successful. Results:\n{output}",
    on_failure_fn=lambda error: f"OCR Recognition failed: {error}",
    is_available_fn=lambda ctx: is_ocr_configured(ctx),
)
