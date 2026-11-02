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

"""Screen geometry shared by the Explorer agent and the ``ask_explorer`` tool.

Explorer candidates use normalized ``[x, y]`` coordinates on a 0-1000 scale;
device actions use pixels.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Any

from artemis.utils.coordinates import denormalize_point, normalize_point
from artemis.utils.vision import SnapshotRef, uses_minimax

#: Fallback screen size when neither the operator observation nor the device
#: context reports one (a common phone portrait resolution).
FALLBACK_SCREEN_SIZE: tuple[int, int] = (1080, 2400)

NORMALIZED_MAX = 1000


def resolve_screen_size(ctx: Any, state: Any) -> tuple[int, int]:
    """Returns ``(width, height)`` in pixels for the current screenshot.

    Precedence: the operator's latest raw observation (which measured the
    screenshot it annotated), then the device context, then the fallback.
    """
    raw = getattr(state, "operator_raw_data", None) or {}
    if uses_minimax(ctx):
        value = raw.get("snapshot") if isinstance(raw, dict) else None
        snapshot = (
            SnapshotRef.model_validate(value)
            if isinstance(value, dict)
            else getattr(ctx, "_current_snapshot", None)
        )
        if (
            not isinstance(snapshot, SnapshotRef)
            or not snapshot.capture_valid
            or min(snapshot.width, snapshot.height) <= 1
        ):
            raise ValueError("MiniMax target conversion requires an actual valid capture")
        return snapshot.width, snapshot.height
    width = raw.get("width") if isinstance(raw, dict) else None
    height = raw.get("height") if isinstance(raw, dict) else None
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return width, height

    device = getattr(ctx, "device", None)
    dev_w = getattr(device, "device_width", None) if device else None
    dev_h = getattr(device, "device_height", None) if device else None
    fb_w, fb_h = FALLBACK_SCREEN_SIZE
    width = dev_w if isinstance(dev_w, int) and dev_w > 0 else fb_w
    height = dev_h if isinstance(dev_h, int) and dev_h > 0 else fb_h
    return width, height


def norm_to_pixel(nx: float, ny: float, width: int, height: int) -> tuple[int, int]:
    """Maps normalized 0-1000 coordinates to clamped pixel coordinates."""
    return tuple(denormalize_point(nx, ny, width, height))


def pixel_to_norm(px: float, py: float, width: int, height: int) -> tuple[int, int]:
    """Maps physical pixels using the shared finite-number and rounding contract."""
    return tuple(normalize_point(px, py, width, height))


def is_valid_norm_point(coords: Any, *, strict: bool = False) -> bool:
    """True when ``coords`` is a 2-item ``[nx, ny]`` inside the 0-1000 range."""
    if not isinstance(coords, (list, tuple)) or len(coords) != 2:
        return False
    if any(isinstance(v, bool) for v in coords):
        return False
    if strict and any(not isinstance(v, Real) for v in coords):
        return False
    try:
        return all(math.isfinite(float(v)) and 0 <= float(v) <= NORMALIZED_MAX for v in coords)
    except (ValueError, TypeError, OverflowError):
        return False
