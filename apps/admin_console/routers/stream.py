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

"""Device Screen Live Streaming Router.

Exposes real-time screen streaming endpoints for the Web UI.

Both routes follow *the task* (``?session_id=`` or the currently active run)
and mirror that task's device only — they never pick an attached device on
their own.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from admin_console.services.device_stream_service import (
        StreamDeviceError,
        StreamDeviceUnavailable,
        device_stream_service,
    )
except ImportError:
    from apps.admin_console.services.device_stream_service import (
        StreamDeviceError,
        StreamDeviceUnavailable,
        device_stream_service,
    )

router = APIRouter(tags=["stream"])

SESSION_QUERY = Query(
    default=None,
    description="Task/session to follow. Defaults to the currently active run.",
)


@router.get("/api/stream/device-live")
async def stream_device_live(session_id: str | None = SESSION_QUERY):
    """Streams live frames of the watched task's device as multipart MJPEG."""
    try:
        device = await device_stream_service.resolve_stream_device(session_id)
        await device_stream_service.validate_online(device.serial)
    except StreamDeviceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StreamDeviceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return StreamingResponse(
        device_stream_service.mjpeg_frame_generator(session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/stream/device-state")
async def get_device_stream_state(session_id: str | None = SESSION_QUERY):
    """Reports whether the watched task's device can be live-streamed.

    Always answers 200 with ``connected``/``error`` so the player can explain
    *why* a stream failed (no task, no device recorded, device offline).
    """
    try:
        device = await device_stream_service.resolve_stream_device(session_id)
        await device_stream_service.validate_online(device.serial)
    except StreamDeviceError as exc:
        return JSONResponse(
            {
                "connected": False,
                "serial": None,
                "session_id": session_id,
                "source": None,
                "error": str(exc),
                "live_stream_url": None,
            }
        )
    return JSONResponse(
        {
            "connected": True,
            "serial": device.serial,
            "session_id": device.session_id,
            "source": device.source,
            "error": None,
            "live_stream_url": "/api/stream/device-live",
        }
    )
