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
import copy
import difflib
from io import BytesIO
from typing import Any

from artemis.utils.ui_filter import _parse_bounds
from third_party.mobile_use.utils.logger import get_logger
from PIL import Image

logger = get_logger(__name__)

PLACEHOLDERS = {"search", "enter text", "placeholder"}


def _is_low_value_text(text: str) -> bool:
    """Check if the text is low value or a placeholder for virtual nodes."""
    if not text:
        return True

    text_strip = text.strip()

    # 1. Extremely short character check
    if len(text_strip) == 1:
        # Check if it's a Chinese character
        is_chinese = "\u4e00" <= text_strip <= "\u9fff"
        # Check if it's a digit
        is_digit = text_strip.isdigit()

        # If it's neither Chinese nor digit, consider it low value (likely noise)
        if not (is_chinese or is_digit):
            return True

    # 2. Common placeholders
    if text_strip.lower() in PLACEHOLDERS:
        return True

    # 3. Default markers from tools
    if text_strip in {"[Icon]", "[Image]", "[Picture]"}:
        return True

    return False


def _parse_ocr_position(position: list[dict[str, int]]) -> dict[str, int]:
    """Convert OCR vertices to [left, top, right, bottom]."""
    if not position:
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}
    left = min(v.get("x", 0) for v in position)
    top = min(v.get("y", 0) for v in position)
    right = max(v.get("x", 0) for v in position)
    bottom = max(v.get("y", 0) for v in position)
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _calculate_overlap_ratio(ocr_bounds: dict[str, int], xml_bounds: dict[str, int]) -> float:
    """Calculate how much of the OCR box is inside the XML node (Intersection / OCR_Area)."""
    inter_left = max(ocr_bounds["left"], xml_bounds["left"])
    inter_top = max(ocr_bounds["top"], xml_bounds["top"])
    inter_right = min(ocr_bounds["right"], xml_bounds["right"])
    inter_bottom = min(ocr_bounds["bottom"], xml_bounds["bottom"])

    if inter_left >= inter_right or inter_top >= inter_bottom:
        return 0.0

    inter_area = (inter_right - inter_left) * (inter_bottom - inter_top)
    ocr_area = (ocr_bounds["right"] - ocr_bounds["left"]) * (
        ocr_bounds["bottom"] - ocr_bounds["top"]
    )

    if ocr_area == 0:
        return 0.0

    return inter_area / ocr_area


def _detect_status_bar_height(xml_hierarchy: list[dict[str, Any]], screen_height: int) -> int:
    """Try to detect status bar height from XML, fallback to 4% of screen height."""
    if type(screen_height) is not int or screen_height <= 0:
        return 0
    for node in xml_hierarchy:
        package = node.get("package") or node.get("packageName")
        bounds_str = node.get("bounds")

        # Look for systemui at the top
        if package == "com.android.systemui":
            bounds = _parse_bounds(bounds_str)
            if bounds and bounds["top"] == 0:
                candidate = bounds["bottom"]
                return candidate if 0 < candidate < screen_height else 0

    return int(screen_height * 0.04)


def _crop_image_remove_status_bar(screenshot_b64: str, crop_height: int) -> tuple[str, int, int]:
    """Crop the top part of the image.

    Returns cropped b64, original width, original height.
    """
    img_data = base64.b64decode(screenshot_b64)
    img = Image.open(BytesIO(img_data))
    width, height = img.size

    if not 0 < crop_height < height:
        logger.debug("Status bar crop is outside the input image. Not cropping.")
        return screenshot_b64, width, height

    # Crop: left, top, right, bottom
    cropped_img = img.crop((0, crop_height, width, height))

    buffered = BytesIO()
    cropped_img.save(buffered, format="PNG")
    cropped_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return cropped_b64, width, height


def _map_coordinates_back(
    ocr_results: list[dict[str, Any]], status_bar_height: int
) -> list[dict[str, Any]]:
    """Map OCR coordinates back to original screen coordinates."""
    mapped_results = []
    for res in ocr_results:
        text = res.get("text", "")
        position = res.get("position", [])

        mapped_position = []
        for v in position:
            mapped_position.append({"x": v.get("x", 0), "y": v.get("y", 0) + status_bar_height})

        mapped = {**res, "text": text, "position": mapped_position, "position_space": "root_pixel"}
        snapshot = res.get("snapshot")
        if (
            isinstance(snapshot, dict)
            and snapshot.get("width", 0) > 0
            and snapshot.get("height", 0) > 0
            and mapped_position
        ):
            from artemis.utils.coordinates import normalize_bbox

            mapped["bbox"] = normalize_bbox(
                [
                    min(v["x"] for v in mapped_position),
                    min(v["y"] for v in mapped_position),
                    max(v["x"] for v in mapped_position),
                    max(v["y"] for v in mapped_position),
                ],
                snapshot["width"],
                snapshot["height"],
            )
        mapped_results.append(mapped)
    return mapped_results


def pure_xml_hierarchy(hierarchy: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Copy native XML while removing previously fused OCR-derived material.

    The historical fuser only added ocr_elements and a class suffix; it never
    rewrote native text/content-desc. Preserve that original text. Explicit OCR
    virtual nodes or OCR-sourced text are not native accessibility evidence.
    Newly recognized typed OCR is fused *after* this baseline is prepared.
    """
    cleaned = []
    derived_keys = {
        "ocr_elements",
        "ocr_text",
        "ocr_result",
        "ocr_results",
        "ocr_status",
        "ocr_version",
        "recognition_source",
        "snapshot",
        "snapshot_id",
        "bbox",
        "transform",
        "coordinate_space",
        "coordinate_version",
        "evidence",
        "status",
    }
    for original in hierarchy or []:
        if not isinstance(original, dict):
            continue
        node = copy.deepcopy(original)
        class_name = str(node.get("class") or node.get("className") or "")
        native_class = class_name.replace("[OCR]", "").strip()
        if (
            ("[OCR]" in class_name and not native_class)
            or str(node.get("is_ocr", "")).lower() == "true"
            or node.get("source") in {"ocr", "minimax"}
            or node.get("text_source") in {"ocr", "minimax"}
        ):
            continue
        for key in derived_keys:
            node.pop(key, None)
        for key in ("class", "className"):
            if isinstance(node.get(key), str):
                node[key] = node[key].replace("[OCR]", "").strip()
        if isinstance(node.get("children"), list):
            node["children"] = pure_xml_hierarchy(node["children"])
        cleaned.append(node)
    return cleaned


def fuse_ocr_with_xml(
    xml_hierarchy: list[dict[str, Any]], ocr_results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fuse OCR results with XML hierarchy with advanced line-grouping aggregation.

    Assumes ocr_results coordinates are already mapped back to full screen.
    """
    # Never persist a fused derivative through an alias to raw capture/DB data.
    xml_hierarchy = copy.deepcopy(xml_hierarchy)
    # 0. Detect screen dimensions to calculate screen area
    max_width = 1080
    max_height = 2400
    for node in xml_hierarchy:
        bounds_str = node.get("bounds")
        xml_bounds = _parse_bounds(bounds_str)
        if xml_bounds:
            max_width = max(max_width, xml_bounds["right"])
            max_height = max(max_height, xml_bounds["bottom"])
    screen_area = max_width * max_height

    # 1. Parse OCR positions and keep in list
    ocr_with_bounds = []
    for ocr in ocr_results:
        bounds = _parse_ocr_position(ocr.get("position", []))
        ocr_with_bounds.append({"ocr": ocr, "bounds": bounds})

    # 2. Match OCR to XML nodes (Banded Overlap + Giant Node Exclusion)
    xml_to_ocr_map = {}
    unmatched_ocrs = []

    for idx, ocr_item in enumerate(ocr_with_bounds):
        ocr_bounds = ocr_item["bounds"]
        ocr_text = ocr_item["ocr"].get("text", "").strip()
        if not ocr_text:
            continue

        band1_candidates = []  # list of (node_idx, area)
        band2_candidates = []  # list of (node_idx, area)

        for node_idx, node in enumerate(xml_hierarchy):
            bounds_str = node.get("bounds")
            xml_bounds = _parse_bounds(bounds_str)
            if not xml_bounds:
                continue

            node_area = (xml_bounds["right"] - xml_bounds["left"]) * (
                xml_bounds["bottom"] - xml_bounds["top"]
            )
            node_class = str(node.get("className") or node.get("class") or "")

            # Exclude Giant Nodes (unless text-centric class)
            is_text_class = any(
                tc in node_class.lower() for tc in ["text", "button", "edit", "search", "input"]
            )
            if node_area > screen_area * 0.1 and not is_text_class:
                continue  # Exclude giant container

            overlap = _calculate_overlap_ratio(ocr_bounds, xml_bounds)

            if overlap >= 0.9:
                band1_candidates.append((node_idx, node_area))
            elif overlap >= 0.7:
                band2_candidates.append((node_idx, node_area))

        best_node_idx = None
        if band1_candidates:
            # Sort by area ascending (prefer smaller/more specific nodes)
            band1_candidates.sort(key=lambda x: x[1])
            best_node_idx = band1_candidates[0][0]
        elif band2_candidates:
            band2_candidates.sort(key=lambda x: x[1])
            best_node_idx = band2_candidates[0][0]

        if best_node_idx is not None:
            if best_node_idx not in xml_to_ocr_map:
                xml_to_ocr_map[best_node_idx] = []
            xml_to_ocr_map[best_node_idx].append(ocr_item)
        else:
            unmatched_ocrs.append(ocr_item)

    # 3. Advanced Aggregation for Matched Nodes (Line Grouping)
    for node_idx, matched_list in xml_to_ocr_map.items():
        node = xml_hierarchy[node_idx]
        xml_text = str(node.get("text") or "").strip()

        # Group items into lines to handle vertical misalignment
        lines = []
        matched_list.sort(key=lambda x: x["bounds"]["top"])

        for item in matched_list:
            b = item["bounds"]
            placed = False
            for line in lines:
                # If vertical center is close to line's average vertical center
                line_center = sum(x["bounds"]["top"] + x["bounds"]["bottom"] for x in line) / (
                    2 * len(line)
                )
                item_center = (b["top"] + b["bottom"]) / 2
                if abs(line_center - item_center) < 10:  # Stricter 10 pixel tolerance for same line
                    line.append(item)
                    placed = True
                    break
            if not placed:
                lines.append([item])

        # Sort lines from top to bottom, and items within line from left to right
        lines.sort(key=lambda row: sum(x["bounds"]["top"] for x in row) / len(row))
        for line in lines:
            line.sort(key=lambda x: x["bounds"]["left"])

        # Flatten and join text
        sorted_items = [item for line in lines for item in line]
        aggregated_ocr_text = " ".join(item["ocr"].get("text", "").strip() for item in sorted_items)

        # Discard Strategy (Deduplication)
        # We synthesize the full text to check for redundancy against XML text
        discard = False
        if xml_text and (aggregated_ocr_text in xml_text):
            discard = True
        if xml_text and not discard:
            similarity = difflib.SequenceMatcher(
                None, xml_text.lower(), aggregated_ocr_text.lower()
            ).ratio()
            if similarity > 0.8:
                discard = True

        if not discard and any(item["ocr"].get("source") == "minimax" for item in matched_list):
            from artemis.utils.vision import copy_provenance

            node["ocr_elements"] = [
                {
                    "text": item["ocr"].get("text", ""),
                    "bounds": f"[{item['bounds']['left']},{item['bounds']['top']}][{item['bounds']['right']},{item['bounds']['bottom']}]",
                    **copy_provenance(item["ocr"]),
                }
                for item in matched_list
            ]
            continue

        if not discard:
            # Instead of a single flat string, we list OCR elements line by line
            ocr_elements = []
            for line in lines:
                current_text = ""
                current_bounds = None

                for item in line:
                    txt = item["ocr"].get("text", "").strip()
                    b = item["bounds"]

                    if not current_bounds:
                        current_bounds = b.copy()
                        current_text = txt
                    else:
                        # Strict horizontal tolerance: if within 30 pixels, merge them
                        if b["left"] - current_bounds["right"] < 30:
                            current_bounds["right"] = b["right"]
                            current_bounds["bottom"] = max(current_bounds["bottom"], b["bottom"])
                            current_text += " " + txt
                        else:
                            # Save current block and start a new one
                            ocr_elements.append(
                                {
                                    "text": current_text,
                                    "bounds": (
                                        f"[{current_bounds['left']},{current_bounds['top']}][{current_bounds['right']},{current_bounds['bottom']}]"
                                    ),
                                }
                            )
                            current_bounds = b.copy()
                            current_text = txt

                # Don't forget the last block in the line
                if current_bounds:
                    ocr_elements.append(
                        {
                            "text": current_text,
                            "bounds": (
                                f"[{current_bounds['left']},{current_bounds['top']}][{current_bounds['right']},{current_bounds['bottom']}]"
                            ),
                        }
                    )

            node["ocr_elements"] = ocr_elements
            orig_class = str(node.get("className") or node.get("class") or "")
            if "[OCR]" not in orig_class:
                node["class"] = f"{orig_class} [OCR]".strip()

    from artemis.utils.vision import copy_provenance

    for item in unmatched_ocrs:
        ocr, bounds = item["ocr"], item["bounds"]
        if ocr.get("source") == "minimax":
            xml_hierarchy.append(
                {
                    "class": "[OCR]",
                    "bounds": f"[{bounds['left']},{bounds['top']}][{bounds['right']},{bounds['bottom']}]",
                    "ocr_elements": [
                        {
                            "text": ocr.get("text", ""),
                            "bounds": f"[{bounds['left']},{bounds['top']}][{bounds['right']},{bounds['bottom']}]",
                            **copy_provenance(ocr),
                        }
                    ],
                }
            )

    return xml_hierarchy
