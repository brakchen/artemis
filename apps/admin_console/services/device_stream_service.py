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

"""Device Live Screen Streaming Service.

Provides real-time, low-latency device screen frames over HTTP MJPEG.
Operates concurrently with ADB agent actions with zero interference.

The stream follows **the task**, never a device list: the watched task
(``session_id``, defaulting to the currently active run) is resolved to *its*
device serial, and only that device is mirrored. When the watched task changes
the capture loop switches with it — several tasks may run on different devices
at the same time. When the watched task has no device information, or its
device is not connected, the endpoints report an error instead of silently
streaming some other attached device.
"""

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from artemis.toolchain import find_adb

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo

from artemis.runtime import trace_store

logger = logging.getLogger("artemis.stream_service")

#: How often the capture loop re-reads the watched task's device. Task switches
#: are picked up within this window without re-resolving on every frame.
DEVICE_FOLLOW_TTL_SECONDS = 0.5

#: Consecutive screencap failures before the stream gives up on the task's
#: device (the device vanished mid-task). Never another device.
CAPTURE_FAILURE_LIMIT = 10


class StreamDeviceError(RuntimeError):
    """No device information for the watched task — the stream must not guess."""


class StreamDeviceUnavailable(StreamDeviceError):
    """The watched task's device is known but not connected/usable right now."""


@dataclass(frozen=True)
class TaskDevice:
    """The device a specific task runs on, plus where that fact came from."""

    session_id: str
    serial: str
    source: str  # active_run | queue | status


class DeviceStreamService:
    """Manages real-time screen capture and distribution to web clients."""

    def __init__(self):
        self._active_listeners = 0
        self._lock = asyncio.Lock()
        self._latest_frame: bytes | None = None
        self._last_frame_time: float = 0.0
        self._is_capturing = False
        self._capture_task: asyncio.Task | None = None
        #: (expiry, wanted session, resolved device) — see ``resolve_stream_device``.
        self._device_cache: tuple[float, str, TaskDevice] | None = None

    # ------------------------------------------------------------------ task
    @staticmethod
    def _watched_session_id(session_id: str | None) -> str:
        """The task the viewer follows: the requested one, else the active run.

        The active run is looked up in-process first and then in the sessions
        table, because tasks launched through the MCP/CLI worker live in another
        process than this HTTP server.
        """
        wanted = str(session_id or "").strip()
        if not wanted:
            wanted = str(getattr(state, "active_session_id", None) or "").strip()
        if not wanted:
            wanted = str(session_repo.get_running_session_id() or "").strip()
        if not wanted:
            raise StreamDeviceError(
                "No running task to follow: the live stream mirrors the device of a "
                "task, and no task is active. Start a task or open its session "
                "(?session_id=<id>)."
            )
        return wanted

    def _device_of_session(self, wanted: str) -> TaskDevice:
        """Resolve one task to its device serial, from task records only.

        ``adb devices`` is deliberately not consulted here: choosing "some
        connected device" is exactly how the stream ended up on the wrong
        phone. Sources, in order: the in-memory run record, the queue row of a
        dispatched-but-not-spawned task, the task's own status.json, then the
        sessions table (written at task start, so it also covers tasks launched
        by another process).
        """
        run = state.active_runs.get(wanted)
        if isinstance(run, dict) and run.get("device_id"):
            return TaskDevice(wanted, str(run["device_id"]), "active_run")

        for item in state.queue_items:
            if (
                isinstance(item, dict)
                and str(item.get("session_id")) == wanted
                and item.get("device_serial")
            ):
                return TaskDevice(wanted, str(item["device_serial"]), "queue")

        known_without_device: list[str] = []

        status = trace_store.read_status(wanted)
        if isinstance(status, dict):
            serial = status.get("device_serial")
            if serial:
                return TaskDevice(wanted, str(serial), "status")
            known_without_device.append("status.json")

        row = session_repo.get_session_by_id(wanted)
        if isinstance(row, dict):
            serial = self._device_id_from_row(row)
            if serial:
                return TaskDevice(wanted, serial, "session_db")
            known_without_device.append("the sessions table")

        if known_without_device:
            raise StreamDeviceError(
                f"Task {wanted} exists ({' and '.join(known_without_device)}) but records "
                "no device; refusing to stream an arbitrary device."
            )
        raise StreamDeviceError(
            f"No task {wanted} is running and no record exists for it, so there is no "
            "device to stream."
        )

    @staticmethod
    def _device_id_from_row(row: dict) -> str | None:
        """``device_info.device_id`` of a sessions-table row, if it carries one."""
        raw = row.get("device_info")
        try:
            info = json.loads(raw) if isinstance(raw, str) else (raw if raw else {})
        except (TypeError, ValueError):
            return None
        if not isinstance(info, dict):
            return None
        serial = info.get("device_id")
        return str(serial).strip() if serial else None

    def _resolve_task_device(self, session_id: str | None) -> TaskDevice:
        """Resolve (and briefly cache) the device of the watched task."""
        wanted = self._watched_session_id(session_id)
        now = time.monotonic()
        cached = self._device_cache
        if cached is not None and cached[0] > now and cached[1] == wanted:
            return cached[2]
        device = self._device_of_session(wanted)
        self._device_cache = (now + DEVICE_FOLLOW_TTL_SECONDS, wanted, device)
        return device

    async def resolve_stream_device(self, session_id: str | None = None) -> TaskDevice:
        """Device of the task being watched.

        Raises:
            StreamDeviceError: the watched task has no device information.
        """
        return self._resolve_task_device(session_id)

    # ------------------------------------------------------------------- adb
    async def list_adb_devices(self) -> dict[str, str]:
        """``{serial: state}`` straight from ``adb devices`` (no selection)."""
        try:
            adb_bin = find_adb()
            proc = await asyncio.create_subprocess_exec(
                adb_bin,
                "devices",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise StreamDeviceUnavailable(f"Could not run adb devices: {exc}") from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or "unknown adb error"
            raise StreamDeviceUnavailable(f"adb devices failed: {detail}")

        devices: dict[str, str] = {}
        for line in stdout.decode(errors="replace").strip().splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2:
                devices[parts[0]] = parts[1]
        return devices

    async def validate_online(self, serial: str) -> None:
        """Fail loudly when the task's device cannot be captured right now."""
        devices = await self.list_adb_devices()
        state_value = devices.get(serial)
        if state_value is None:
            attached = ", ".join(devices) or "none"
            raise StreamDeviceUnavailable(
                f"The task's device {serial} is not attached to adb (connected: {attached})."
            )
        if state_value != "device":
            raise StreamDeviceUnavailable(
                f"The task's device {serial} is {state_value}; waiting for it to come "
                "back before streaming."
            )

    # ---------------------------------------------------------------- capture
    async def _capture_loop(self, session_id: str | None):
        """Background frame capture loop while listeners > 0.

        Always mirrors the watched task's current device. If the task ends or
        its device disappears, the loop stops — it never falls back to another
        attached device.
        """
        logger.info("[StreamService] Starting live screen capture loop...")
        current_serial: str | None = None
        failures = 0
        while self._active_listeners > 0:
            try:
                device = self._resolve_task_device(session_id)
            except StreamDeviceError as exc:
                logger.error(f"[StreamService] {exc}; stopping the live stream.")
                break

            if device.serial != current_serial:
                logger.info(
                    f"[StreamService] Following task {device.session_id} → device "
                    f"{device.serial} (source: {device.source})."
                )
                current_serial = device.serial
                failures = 0

            try:
                start_t = time.time()
                adb_bin = find_adb()
                proc = await asyncio.create_subprocess_exec(
                    adb_bin,
                    "-s",
                    device.serial,
                    "exec-out",
                    "screencap",
                    "-p",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0 and len(stdout) > 1000:
                    self._latest_frame = stdout
                    self._last_frame_time = time.time()
                    failures = 0
                else:
                    failures += 1
                    if failures == 1:
                        detail = stderr.decode(errors="replace").strip()
                        logger.warning(
                            f"[StreamService] screencap failed on {device.serial}: "
                            f"{detail or 'no frame'}"
                        )
                    if failures >= CAPTURE_FAILURE_LIMIT:
                        logger.error(
                            f"[StreamService] {CAPTURE_FAILURE_LIMIT} consecutive capture "
                            f"failures on the task's device {device.serial}; stopping the "
                            "live stream (no other device is used)."
                        )
                        break

                elapsed = time.time() - start_t
                delay = max(0.03, 0.08 - elapsed)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"[StreamService] Capture error: {e}")
                await asyncio.sleep(0.5)

        logger.info("[StreamService] Stopping live screen capture loop.")
        self._is_capturing = False

    async def start_capturing(self, session_id: str | None = None):
        """Register a new listener and start background capture if needed."""
        async with self._lock:
            self._active_listeners += 1
            if not self._is_capturing or self._capture_task is None or self._capture_task.done():
                self._is_capturing = True
                self._capture_task = asyncio.create_task(self._capture_loop(session_id))

    async def stop_capturing(self):
        """Deregister a listener and stop capture loop when count reaches 0."""
        async with self._lock:
            self._active_listeners = max(0, self._active_listeners - 1)
            if self._active_listeners == 0 and self._capture_task and not self._capture_task.done():
                self._capture_task.cancel()
                self._is_capturing = False

    async def mjpeg_frame_generator(
        self, session_id: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """Async generator streaming MJPEG multipart bytes to HTTP response."""
        await self.start_capturing(session_id)
        try:
            last_sent_time = 0.0
            while True:
                if self._capture_task is not None and self._capture_task.done():
                    # The capture loop stopped (task ended / its device vanished).
                    # Close the response so the client sees a broken stream instead
                    # of a frozen frame, then asks /api/stream/device-state why.
                    logger.warning("[StreamService] Capture loop ended; closing the live stream.")
                    break
                if self._latest_frame and self._last_frame_time > last_sent_time:
                    last_sent_time = self._last_frame_time
                    frame_bytes = self._latest_frame
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/png\r\n"
                        b"Content-Length: "
                        + str(len(frame_bytes)).encode()
                        + b"\r\n\r\n"
                        + frame_bytes
                        + b"\r\n"
                    )
                await asyncio.sleep(0.04)
        finally:
            await self.stop_capturing()


device_stream_service = DeviceStreamService()
