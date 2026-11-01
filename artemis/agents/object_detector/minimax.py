"""MiniMax localization through the existing utility role and shared budget."""

from __future__ import annotations

import asyncio
from io import BytesIO

from PIL import Image

from artemis.llm.reliability import logical_call_scope
from artemis.services.llm import get_llm
from artemis.utils.coordinates import denormalize_bbox
from artemis.utils.vision import ImageTransform, candidate_from_bbox, snapshot_for_image
from artemis.utils.vision_models import DetectionOutput
from artemis.utils.vision_requests import complete_visual


def _review_crop(image: Image.Image, output: DetectionOutput):
    """One context-preserving region covering all candidates, with generous padding."""
    if not output.candidates:
        return None
    boxes = [denormalize_bbox(c.bbox, *image.size) for c in output.candidates]
    left, t = min(b[0] for b in boxes), min(b[1] for b in boxes)
    r, b = max(b[2] for b in boxes), max(b[3] for b in boxes)
    w, h = image.size
    # At least half the page in each dimension preserves neighboring controls.
    cw, ch = min(w, max(w / 2, (r - left) * 3)), min(h, max(h / 2, (b - t) * 3))
    x = max(0, min(w - cw, (left + r - cw) / 2))
    y = max(0, min(h - ch, (t + b - ch) / 2))
    bounds = (int(x), int(y), int(x + cw), int(y + ch))
    if bounds == (0, 0, w, h):
        return None
    return bounds


async def run_minimax_detection(
    ctx,
    image_data: bytes,
    queries: list[str],
    *,
    global_timeout: float,
    snapshot=None,
    transform=None,
) -> dict:
    snapshot = snapshot or snapshot_for_image(ctx, image_data)
    root_transform = transform or ImageTransform()
    with Image.open(BytesIO(image_data)) as source:
        image = source.copy()
    llm = get_llm(ctx, "object_detector", is_utils=True)
    results = []
    # Sequential queries share the logical deadline and attempt budget. There is
    # no template loop or detached per-query retry owner.
    with logical_call_scope():
        async with asyncio.timeout(global_timeout):
            for query in queries:
                prompt = (
                    "Locate the actionable UI target described below in the entire input image. "
                    "Return status found with exactly one candidate only if uniquely identified; "
                    "ambiguous with at least two distinct candidates if unresolved; not_found with none if absent. "
                    "Only provide status and candidates containing label and bbox=[left,top,right,bottom]. "
                    "Coordinates are x,y on a 0..1000 grid of this actual image. Do not exchange axes. "
                    f"Target: {query}"
                )
                output = await complete_visual(llm, image_data, prompt, DetectionOutput)
                transform_used = ImageTransform()
                input_size = image.size
                small = output.status == "found" and any(
                    (c.bbox[2] - c.bbox[0]) * (c.bbox[3] - c.bbox[1]) < 1600
                    for c in output.candidates
                )
                if output.status == "ambiguous" or small:
                    crop = _review_crop(image, output)
                    if crop is not None:
                        region = image.crop(crop)
                        stream = BytesIO()
                        region.save(stream, format="PNG")
                        output = await complete_visual(
                            llm,
                            stream.getvalue(),
                            prompt
                            + " This is one context-preserving crop of the page. Resolve the target using visible context; do not guess.",
                            DetectionOutput,
                        )
                        transform_used = ImageTransform(offset_x=crop[0], offset_y=crop[1])
                        input_size = region.size
                composed = root_transform.compose(transform_used)
                root_size = (snapshot.width, snapshot.height)
                # Unbound standalone images still have known actual dimensions.
                mapped = [
                    candidate_from_bbox(
                        c.label,
                        composed.map_bbox(c.bbox, input_size, root_size),
                        snapshot,
                        transform=composed,
                        status=output.status,
                    )
                    for c in output.candidates
                ]
                results.append({"query": query, "status": output.status, "candidates": mapped})
    return {
        "detected": [
            dict(c, label=r["query"], detected_label=c["label"])
            for r in results
            if r["status"] == "found"
            for c in r["candidates"]
        ],
        "failed": [r["query"] for r in results if r["status"] == "not_found"],
        "ambiguous": [r for r in results if r["status"] == "ambiguous"],
        "results": results,
    }
