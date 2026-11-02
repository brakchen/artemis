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

import os
from typing import Any

import httpx
from artemis.config import settings
from artemis.llm.reliability import LLMCallError
from artemis.utils.vision import uses_minimax
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)
_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT


def is_ocr_configured(ctx=None) -> bool:
    """MiniMax uses the resolved visual default; legacy uses Google Vision."""
    if uses_minimax(ctx):
        return True
    ocr_secret = settings.get_api_key("ocr")
    if ocr_secret and ocr_secret.get_secret_value().strip():
        val = ocr_secret.get_secret_value().strip()
        if val not in ("API_KEY", "your_google_cloud_vision_api_key_here"):
            return True
    for env_var in ("OCR_API_KEY", "VISION_API_KEY"):
        val = os.environ.get(env_var)
        if (
            val
            and val.strip()
            and val.strip() not in ("API_KEY", "your_google_cloud_vision_api_key_here")
        ):
            return True
    return False


def automatic_ocr_enabled(ctx=None) -> bool:
    """Key presence does not opt a MiniMax task into recurring paid OCR."""
    value = getattr(getattr(getattr(ctx, "agent_config", None), "ocr", None), "automatic", None)
    return value if type(value) is bool else not uses_minimax(ctx)


def ocr_outcome(ctx, *, disabled_reason: str | None = None) -> dict:
    if disabled_reason:
        return {"status": "disabled", "reason": disabled_reason}
    value = getattr(ctx, "_ocr_last_outcome", None)
    return dict(value) if isinstance(value, dict) else {}


def ocr_status_text(ctx, outcome: dict | None = None) -> str:
    """Describe this request's typed outcome without changing legacy tool text."""
    if not uses_minimax(ctx):
        return ""
    outcome = dict(outcome) if isinstance(outcome, dict) else ocr_outcome(ctx)
    status = outcome.get("status", "disabled")
    source = outcome.get("source") or "minimax"
    explanations = {
        "partial": "Recognition is incomplete; missing regions do not confirm absence of text.",
        "complete": "OCR request completed; recognition accuracy and full coverage are not guaranteed.",
        "empty": "The OCR request returned confirmed empty regions for this input.",
        "disabled": "OCR was not performed; the displayed hierarchy uses XML only.",
        "error": "OCR failed; the displayed hierarchy uses XML only, not a confirmed empty OCR result.",
    }
    reason = f" Reason: {outcome['reason']}." if outcome.get("reason") else ""
    return f"[OCR status: {status}; source: {source}. {explanations.get(status, 'OCR outcome is unknown.')}{reason}]"


async def perform_ocr(
    screenshot_b64: str,
    client: httpx.AsyncClient | None = None,
    *,
    ctx=None,
    snapshot=None,
    transform=None,
) -> list[dict[str, Any]]:
    """Calls Google Vision API for text recognition on an image.

    Args:
        screenshot_b64: Base64 encoded screenshot image.
        client: Optional persistent httpx.AsyncClient.

    Returns:
        A list of dictionaries containing detected text and position vertices.
    """
    if uses_minimax(ctx):
        from artemis.utils.visual_ocr import recognize_ocr, ocr_to_legacy

        try:
            result = await recognize_ocr(
                screenshot_b64, ctx=ctx, snapshot=snapshot, transform=transform
            )
        except (LLMCallError, OSError, ValueError) as error:
            ctx._ocr_last_outcome = {
                "status": "error",
                "reason": type(error).__name__,
                "source": "minimax",
                "snapshot_id": getattr(snapshot, "capture_id", None),
            }
            raise
        ctx._ocr_last_outcome = {
            "status": result.status,
            "source": result.source,
            "schema_version": result.schema_version,
            "cache_key": result.cache_key,
            "cache_hit": result.cache_hit,
            "snapshot_id": result.snapshot.capture_id,
            "image_hash": result.snapshot.image_hash,
        }
        return ocr_to_legacy(result)

    ocr_secret = settings.get_api_key("ocr")
    api_key = (
        (ocr_secret.get_secret_value() if ocr_secret else None)
        or os.environ.get("OCR_API_KEY")
        or os.environ.get("VISION_API_KEY")
    )
    if not api_key:
        return []

    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = {
        "requests": [
            {
                "image": {"content": screenshot_b64},
                "features": [{"type": "TEXT_DETECTION"}],
            }
        ]
    }

    active_client = client if client is not None else get_http_client()
    response = await active_client.post(url, json=data, headers=headers, timeout=30.0)
    return _parse_ocr_response(response)


def _parse_ocr_response(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code != 200:
        # An upstream body may echo the request key or other private input.
        raise ValueError(f"Vision API returned status {response.status_code}")
    res_json = response.json()
    if not isinstance(res_json, dict):
        raise ValueError("Vision API response must be an object")
    responses = res_json.get("responses", [])
    if not isinstance(responses, list) or any(not isinstance(row, dict) for row in responses):
        raise ValueError("Vision API responses must be a list of objects")
    if not responses or "textAnnotations" not in responses[0]:
        return []
    annotations = responses[0]["textAnnotations"]
    if not isinstance(annotations, list) or any(not isinstance(ann, dict) for ann in annotations):
        raise ValueError("Vision API text annotations must be a list of objects")
    results = []
    # Skip the first annotation (index 0) as it is the full-screen combined text.
    for ann in annotations[1:]:
        desc = ann.get("description", "")
        polygon = ann.get("boundingPoly", {})
        if not isinstance(desc, str) or not isinstance(polygon, dict):
            raise ValueError("Vision API annotation text and polygon have invalid shapes")
        vertices = polygon.get("vertices", [])
        if not isinstance(vertices, list) or any(
            not isinstance(vertex, dict) for vertex in vertices
        ):
            raise ValueError("Vision API annotation vertices must be a list of objects")
        results.append({"text": desc, "position": vertices})
    return results
