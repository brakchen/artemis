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

from typing import Any

from pydantic import BaseModel, Field
from artemis.utils.cython_compat import CyFunctionDetector
from artemis.utils.vision import SnapshotRef


class ScreenDataResponse(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    base64: str = Field(default="", description="Base64 encoded screenshot string")
    elements: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed UI hierarchy elements"
    )
    width: int = Field(default=1080, description="Device screen width in pixels")
    height: int = Field(default=2400, description="Device screen height in pixels")
    device_id: str | None = None
    display_id: str = "0"
    rotation: int | None = None
    capture_valid: bool = True
    invalid_reason: str | None = None
    snapshot: SnapshotRef | None = None
    platform: str = Field(default="android", description="Platform identifier")
