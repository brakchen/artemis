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

"""Android Emulator Launch & Boot Progress Lifecycle Manager."""

import asyncio
from collections import deque
from enum import Enum
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from artemis.platform import OSType, platform
from artemis.toolchain import toolchain
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Connectivity-probe endpoints the guest uses to decide "this network reaches
#: the internet". The AOSP defaults probe `www.google.com`, which is unreachable
#: from a network that cannot route there: both probes wait out the 10s connect
#: timeout, Android marks the network PARTIAL_CONNECTIVITY instead of VALIDATED,
#: and every app that checks that capability reports "no network".
#: `connectivitycheck.gstatic.com` answers 204 wherever the guest has IPv4
#: egress, so validation passes.
_CAPTIVE_PORTAL_HTTPS_URL = "https://connectivitycheck.gstatic.com/generate_204"
_CAPTIVE_PORTAL_HTTP_URL = "http://connectivitycheck.gstatic.com/generate_204"

#: Interfaces whose IPv6 is switched off after boot. The slirp link carries no
#: IPv6 route while DNS still answers with bogus AAAA records (poisoned
#: `www.google.com -> 2001::1`), so every dual-stack connect stalls on the
#: blackhole address before IPv4 can be tried.
_IPV6_DISABLE_INTERFACES = ("all", "default", "eth0")

#: Pause between bouncing the emulated radio to re-run network validation.
_REVALIDATE_PAUSE_SECONDS = 2.0


async def _run_adb(adb_path: str, serial: str, *args: str, timeout: float = 8.0) -> bool:
    """Run one adb command; ``True`` only when it exited 0. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            adb_path,
            "-s",
            serial,
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug(f"[EmulatorManager] adb {' '.join(args)} on {serial} failed: {exc}")
        return False


async def configure_emulator_network(adb_path: str, serial: str) -> list[str]:
    """Make a freshly booted emulator's network actually validate.

    Applies two guest-side fixes and re-runs validation:

    1. repoint the captive-portal probes at a host that answers (see
       ``_CAPTIVE_PORTAL_*``) — without this the guest stays
       PARTIAL_CONNECTIVITY and apps report "network unavailable";
    2. best-effort IPv6 shutdown on the slirp link so poisoned AAAA answers
       fail fast instead of hanging connects.

    Emulators only — a physical phone keeps its own validated network and must
    never have its global settings rewritten. Every step logs and continues, so
    a locked-down image can only lose a nicety, never reachability or READY.

    Returns the actions applied (used for boot logs and tests).
    """
    if not serial.startswith("emulator-"):
        return []

    applied: list[str] = []
    for key, value in (
        ("captive_portal_https_url", _CAPTIVE_PORTAL_HTTPS_URL),
        ("captive_portal_http_url", _CAPTIVE_PORTAL_HTTP_URL),
    ):
        if await _run_adb(adb_path, serial, "shell", "settings", "put", "global", key, value):
            applied.append(key)

    # `adb root` restarts adbd on google_apis images (and is refused elsewhere);
    # wait for it to come back before using the root-only sysctl.
    if await _run_adb(adb_path, serial, "root", timeout=15.0):
        for _ in range(10):
            await asyncio.sleep(1.0)
            if await _run_adb(adb_path, serial, "shell", "echo", "ok", timeout=4.0):
                break
        for iface in _IPV6_DISABLE_INTERFACES:
            key = f"net.ipv6.conf.{iface}.disable_ipv6"
            if await _run_adb(adb_path, serial, "shell", "sysctl", "-w", f"{key}=1"):
                applied.append(key)

    # The first validation already ran before these settings existed and
    # Android does not re-read them on its own: bounce the emulated radio so a
    # fresh network agent probes with the new URLs.
    if await _run_adb(adb_path, serial, "shell", "svc", "data", "disable"):
        await asyncio.sleep(_REVALIDATE_PAUSE_SECONDS)
        await _run_adb(adb_path, serial, "shell", "svc", "data", "enable")
        applied.append("revalidated")

    logger.info(
        f"[EmulatorManager] Network validation configured on {serial}: "
        f"{', '.join(applied) if applied else 'nothing applied'}"
    )
    return applied


class EmulatorLaunchStage(str, Enum):
    """Lifecycle stages of launching an Android Virtual Device."""

    IDLE = "idle"
    STARTING = "starting"  # Process spawned, verifying PID
    WAITING_FOR_ADB = "waiting_for_adb"  # Process alive, waiting for emulator to appear in ADB
    BOOTING = "booting"  # Visible in ADB, waiting for sys.boot_completed=1
    READY = "ready"  # Boot complete and ready for automation
    FAILED = "failed"  # Process crashed, lock error, or timeout
    BUSY = "busy"  # Refused: another emulator instance is already running
    STOPPED = "stopped"  # Stopped by user


class EmulatorLaunchState(BaseModel):
    """State schema for real-time emulator launch tracking."""

    avd_name: str | None = None
    status: EmulatorLaunchStage = EmulatorLaunchStage.IDLE
    pid: int | None = None
    serial: str | None = None
    stage_message: str = "Ready to launch"
    progress_percent: int = 0
    started_at: float | None = None
    elapsed_seconds: int = 0
    error: str | None = None
    logs: list[str] = Field(default_factory=list)
    can_retry: bool = True


def _describe_crash(avd_name: str, poll_res: int | None, logs_str: str) -> str:
    """Turn a raw emulator crash into an actionable message.

    The Android emulator prints a FATAL line that is much more informative than
    "exit code 1", so surface the known cases verbatim-ish.
    """
    low = logs_str.lower()
    if "same avd" in low or "read-only flag" in low:
        return (
            f"AVD '{avd_name}' is already running - Android allows only one emulator "
            "instance per AVD. Create a second AVD (Installed Virtual Devices -> Add) "
            "to launch another virtual device, or stop the existing instance first."
        )
    if "already running" in low or "lock" in low:
        return (
            f"AVD '{avd_name}' is locked by another running emulator instance. "
            "Stop the existing instance or restart ADB."
        )
    if "panic" in low:
        return f"Emulator panic: {logs_str}"
    if "broken avd system path" in low:
        return "Emulator cannot resolve the SDK root - make sure <SDK>/platform-tools exists."
    return f"Emulator process exited with code {poll_res}."


def _current_emulator_serials(adb_path: str) -> set[str]:
    """Serials of emulator instances already attached to adb."""
    try:
        out = subprocess.run(
            [adb_path, "devices"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:  # pylint: disable=broad-exception-caught
        return set()
    return {
        line.split()[0]
        for line in out.splitlines()
        if line.strip().startswith("emulator-") and line.split()
    }


class EmulatorManager:
    """Manages background emulator execution, stdout/stderr stream capture, and boot completion polling."""

    def __init__(self):
        self._current_state = EmulatorLaunchState()
        self._proc: subprocess.Popen | None = None
        self._log_buffer: deque[str] = deque(maxlen=100)
        self._track_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._reader_thread: threading.Thread | None = None
        self._preexisting_serials: set[str] = set()

    @staticmethod
    def _subprocess_creation_kwargs() -> dict[str, Any]:
        """Isolate the emulator from the UI server's console signals.

        ``start_new_session`` is POSIX-only and is ignored by Python's Windows
        subprocess implementation. Windows therefore needs explicit creation
        flags so Ctrl+C or shutdown signals sent to the UI server do not also
        terminate the emulator. The Linux/macOS behavior remains unchanged.
        """
        if sys.platform == "win32":
            return {
                "creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            }
        return {"start_new_session": True}

    @staticmethod
    def _emulator_env() -> dict[str, str]:
        """Environment handed to the emulator process.

        The AVD renders with ``hw.gpu.mode=host`` (host GPU instead of the
        SwiftShader software rasterizer), and that path needs a GL/X display —
        while the UI server itself runs headless in a service with no DISPLAY.
        Without one the emulator dies with ``Failed to get EGL display`` and
        never reaches adb, so the logged-in desktop's Xwayland is reused here.
        The cookie file name carries a per-login random suffix, hence the glob
        at launch time. If no desktop session exists the emulator gets no
        display, prints its GPU error and falls back to software rendering.
        """
        env = os.environ.copy()
        if env.get("DISPLAY"):
            return env
        try:
            cookies = sorted(Path(f"/run/user/{os.getuid()}").glob(".mutter-Xwaylandauth.*"))
        except OSError:
            cookies = []
        if not cookies:
            return env
        env["DISPLAY"] = ":0"
        env["XAUTHORITY"] = str(cookies[-1])
        return env

    def _locate_emulator(self) -> str | None:
        """Find the emulator binary path from PATH or standard SDK environments."""
        resolved = toolchain.resolve("emulator")
        if resolved:
            return resolved

        emu_path = shutil.which("emulator")
        if emu_path:
            return emu_path

        sdk_candidates = [
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
            str(Path.home() / "Android" / "Sdk"),
            str(Path.home() / "Android" / "sdk"),
            str(Path.home() / "Library" / "Android" / "sdk"),
            os.getenv("LOCALAPPDATA", "") + "/Android/Sdk" if os.getenv("LOCALAPPDATA") else None,
            "/usr/lib/android-sdk",
            "/opt/android-sdk",
        ]
        for base in sdk_candidates:
            if base:
                cand = (
                    Path(base)
                    / "emulator"
                    / ("emulator.exe" if platform.os_type == OSType.WINDOWS else "emulator")
                )
                if cand.is_file():
                    return str(cand)
        return None

    def _locate_adb(self) -> str:
        """Find the adb binary path."""
        resolved = toolchain.resolve("adb")
        if resolved:
            return resolved
        return shutil.which("adb") or "adb"

    def get_status(self) -> EmulatorLaunchState:
        """Return current launch progress state snapshot."""
        state = self._current_state.model_copy()
        if state.started_at and state.status in (
            EmulatorLaunchStage.STARTING,
            EmulatorLaunchStage.WAITING_FOR_ADB,
            EmulatorLaunchStage.BOOTING,
        ):
            state.elapsed_seconds = int(time.time() - state.started_at)
        state.logs = list(self._log_buffer)
        return state

    def _stream_logs(self, proc: subprocess.Popen):
        """Continuously read process stdout/stderr in a background thread."""
        try:
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    clean_line = line.strip()
                    if clean_line:
                        self._log_buffer.append(clean_line)
        except Exception as e:
            logger.debug(f"Log streaming finished: {e}")
        finally:
            if proc.stdout:
                proc.stdout.close()

    async def launch(self, avd_name: str) -> EmulatorLaunchState:
        """Initiate background emulator launch and start asynchronous boot monitoring."""
        clean_avd = avd_name.strip()
        if not clean_avd:
            self._current_state = EmulatorLaunchState(
                status=EmulatorLaunchStage.FAILED,
                error="AVD name cannot be empty.",
                stage_message="Launch failed: AVD name cannot be empty.",
            )
            return self.get_status()

        async with self._lock:
            # If already launching the same AVD and process is alive
            if (
                self._current_state.status
                in (
                    EmulatorLaunchStage.STARTING,
                    EmulatorLaunchStage.WAITING_FOR_ADB,
                    EmulatorLaunchStage.BOOTING,
                )
                and self._current_state.avd_name == clean_avd
                and self._proc is not None
                and self._proc.poll() is None
            ):
                logger.info(f"AVD '{clean_avd}' is already in progress of launching.")
                return self.get_status()

            emu_path = self._locate_emulator()
            if not emu_path:
                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.FAILED,
                    error="Android emulator executable not found in PATH or Android SDK. Please install Android SDK platform tools and emulator.",
                    stage_message="Emulator binary not found.",
                )
                return self.get_status()

            self._log_buffer.clear()
            self._log_buffer.append(f"Starting emulator '{clean_avd}' using binary: {emu_path}")

            try:
                # Cancel previous tracker if any
                if self._track_task and not self._track_task.done():
                    self._track_task.cancel()

                # Single-VM policy: this host has limited RAM, so refuse to start a
                # second emulator and report BUSY so the UI can show a notice.
                running_serials = _current_emulator_serials(self._locate_adb())
                if self._proc is not None and self._proc.poll() is None:
                    running_serials.add(f"pid:{self._proc.pid}")
                if running_serials:
                    detail = ", ".join(sorted(running_serials))
                    self._current_state = EmulatorLaunchState(
                        avd_name=clean_avd,
                        status=EmulatorLaunchStage.BUSY,
                        stage_message="An emulator instance is already running.",
                        error=(
                            "Only one emulator can run at a time on this host "
                            f"(memory limit). Already running: {detail}. "
                            "Stop it first, or keep using the running instance."
                        ),
                        can_retry=False,
                        logs=list(self._log_buffer),
                    )
                    logger.info(f"[EmulatorManager] Rejected launch of '{clean_avd}': {detail}")
                    return self.get_status()

                # Emulator serials already attached before this launch: the boot
                # tracker must not adopt an already-running instance (with two VMs
                # up, `adb devices` lists both and the first line may be the other).
                self._preexisting_serials = _current_emulator_serials(self._locate_adb())

                # Spawn emulator process capturing stdout & stderr
                proc = subprocess.Popen(
                    # Headless: Artemis captures the screen through scrcpy/adb, so no
                    # window is needed. This also removes the Qt xcb/display dependency
                    # (a service context has no DISPLAY, and libxcb-cursor may be absent).
                    [
                        emu_path,
                        "-avd",
                        clean_avd,
                        "-no-window",
                        "-no-audio",
                        "-no-boot-anim",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self._emulator_env(),
                    **self._subprocess_creation_kwargs(),
                )
                self._proc = proc
                now = time.time()

                # Start reader thread for stdout/stderr
                self._reader_thread = threading.Thread(
                    target=self._stream_logs, args=(proc,), daemon=True
                )
                self._reader_thread.start()

                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.STARTING,
                    pid=proc.pid,
                    stage_message=f"Process spawned (PID: {proc.pid}). Initializing QEMU virtualization...",
                    progress_percent=15,
                    started_at=now,
                    elapsed_seconds=0,
                    error=None,
                    logs=list(self._log_buffer),
                )

                # Launch async boot tracker task
                self._track_task = asyncio.create_task(
                    self._track_boot_lifecycle(clean_avd, proc, now)
                )

                return self.get_status()

            except Exception as e:
                logger.error(f"Failed to spawn emulator process: {e}")
                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.FAILED,
                    error=f"Failed to spawn emulator process: {e}",
                    stage_message="Failed to spawn emulator process.",
                    logs=list(self._log_buffer),
                )
                return self.get_status()

    async def _track_boot_lifecycle(self, avd_name: str, proc: subprocess.Popen, started_at: float):
        """Monitor emulator lifecycle from process execution to ADB connection and OS boot completion."""
        adb_path = self._locate_adb()
        detected_serial: str | None = None
        max_wait_seconds = 180  # 3 minutes maximum boot timeout

        try:
            # Phase 1: Early crash detection (first 5 seconds)
            for _ in range(5):
                await asyncio.sleep(1)
                poll_res = proc.poll()
                if poll_res is not None:
                    logs_str = "\n".join(list(self._log_buffer)[-10:])
                    error_msg = _describe_crash(avd_name, poll_res, logs_str)

                    logger.error(f"[EmulatorManager] Early crash: {error_msg}")
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=error_msg,
                        stage_message=f"Process exited prematurely (Exit code {poll_res})",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

            # Phase 2: Waiting for ADB handshake
            self._current_state.status = EmulatorLaunchStage.WAITING_FOR_ADB
            self._current_state.progress_percent = 35
            self._current_state.stage_message = (
                "QEMU hypervisor started. Waiting for ADB connection..."
            )

            while time.time() - started_at < max_wait_seconds:
                await asyncio.sleep(1.5)

                # Check if process died
                poll_res = proc.poll()
                if poll_res is not None:
                    logs_str = "\n".join(list(self._log_buffer)[-10:])
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=_describe_crash(avd_name, poll_res, logs_str),
                        stage_message="Process terminated unexpectedly",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

                # Check adb devices for emulator serial
                try:
                    p = await asyncio.create_subprocess_exec(
                        adb_path,
                        "devices",
                        "-l",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    stdout, _ = await p.communicate()
                    out_text = stdout.decode(errors="replace")

                    # Scan for emulator-*
                    for line in out_text.splitlines():
                        line = line.strip()
                        if line.startswith("emulator-"):
                            parts = line.split()
                            if parts and parts[0] not in self._preexisting_serials:
                                detected_serial = parts[0]
                                break
                except Exception as e:
                    logger.debug(f"ADB query error during boot tracking: {e}")

                if detected_serial:
                    break

            if not detected_serial:
                self._current_state = EmulatorLaunchState(
                    avd_name=avd_name,
                    status=EmulatorLaunchStage.FAILED,
                    pid=proc.pid,
                    error="Timeout waiting for emulator to connect to ADB.",
                    stage_message="ADB handshake timed out.",
                    started_at=started_at,
                    elapsed_seconds=int(time.time() - started_at),
                    logs=list(self._log_buffer),
                )
                return

            # Phase 3: Android OS Booting (Polling sys.boot_completed)
            self._current_state.status = EmulatorLaunchStage.BOOTING
            self._current_state.serial = detected_serial
            self._current_state.progress_percent = 65
            self._current_state.stage_message = (
                f"Connected to ADB ({detected_serial}). Android OS is booting up..."
            )

            while time.time() - started_at < max_wait_seconds:
                await asyncio.sleep(2)

                # Check if process died
                poll_res = proc.poll()
                if poll_res is not None:
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=f"Emulator process crashed during OS boot (exit code {poll_res}).",
                        stage_message="Process crashed during OS boot",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

                # Check sys.boot_completed
                try:
                    p = await asyncio.create_subprocess_exec(
                        adb_path,
                        "-s",
                        detected_serial,
                        "shell",
                        "getprop sys.boot_completed",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    stdout, _ = await p.communicate()
                    boot_completed = stdout.decode(errors="replace").strip()

                    if boot_completed == "1":
                        logger.info(
                            f"[EmulatorManager] Android system boot completed for {detected_serial} ({avd_name})"
                        )
                        # Fix guest-side network validation *before* announcing
                        # READY: tasks must never start against a network Android
                        # still counts as PARTIAL_CONNECTIVITY (apps then report
                        # "no network" even though IPv4 egress is fine).
                        try:
                            applied = await asyncio.wait_for(
                                configure_emulator_network(adb_path, detected_serial),
                                timeout=45.0,
                            )
                            if applied:
                                self._log_buffer.append(
                                    f"Network validation configured: {', '.join(applied)}"
                                )
                        except Exception as e:
                            logger.debug(
                                f"[EmulatorManager] Network configuration skipped on "
                                f"{detected_serial}: {e}"
                            )
                        self._current_state = EmulatorLaunchState(
                            avd_name=avd_name,
                            status=EmulatorLaunchStage.READY,
                            pid=proc.pid,
                            serial=detected_serial,
                            stage_message=f"Android emulator '{avd_name}' is fully booted and ready!",
                            progress_percent=100,
                            started_at=started_at,
                            elapsed_seconds=int(time.time() - started_at),
                            error=None,
                            logs=list(self._log_buffer),
                        )
                        try:
                            from artemis.core.diagnostics.engine import readiness_engine

                            # Focus the readiness report on the freshly booted
                            # emulator; task routing still resolves its own
                            # target from the request or the device pool.
                            readiness_engine.set_probe_target_serial(detected_serial)
                        except Exception as e:
                            logger.debug(f"Failed to focus probes on the new emulator: {e}")
                        return
                    else:
                        # Gradual progress visual indicator
                        elapsed = time.time() - started_at
                        calc_progress = min(95, int(65 + (elapsed / 60) * 30))
                        self._current_state.progress_percent = calc_progress
                        self._current_state.stage_message = f"Android OS booting ({int(elapsed)}s)... Initializing system services..."
                except Exception as e:
                    logger.debug(f"Error querying boot_completed: {e}")

            # Timeout after max_wait_seconds
            self._current_state = EmulatorLaunchState(
                avd_name=avd_name,
                status=EmulatorLaunchStage.FAILED,
                pid=proc.pid,
                serial=detected_serial,
                error=f"Android OS boot did not finish within {max_wait_seconds}s timeout.",
                stage_message="Boot process timed out.",
                started_at=started_at,
                elapsed_seconds=int(time.time() - started_at),
                logs=list(self._log_buffer),
            )

        except asyncio.CancelledError:
            logger.info(f"[EmulatorManager] Boot tracker cancelled for '{avd_name}'")
        except Exception as exc:
            logger.error(f"[EmulatorManager] Unexpected exception in boot tracker: {exc}")
            self._current_state = EmulatorLaunchState(
                avd_name=avd_name,
                status=EmulatorLaunchStage.FAILED,
                pid=proc.pid if proc else None,
                error=f"Unexpected error: {exc}",
                stage_message="Unexpected error during boot tracking.",
                started_at=started_at,
                elapsed_seconds=int(time.time() - started_at),
                logs=list(self._log_buffer),
            )

    async def stop(self) -> dict[str, Any]:
        """Terminate the active emulator process."""
        async with self._lock:
            if self._track_task and not self._track_task.done():
                self._track_task.cancel()

            serial = self._current_state.serial
            pid = self._current_state.pid
            avd = self._current_state.avd_name

            if serial:
                try:
                    adb_path = self._locate_adb()
                    subprocess.run(
                        [adb_path, "-s", serial, "emu", "kill"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                except (OSError, subprocess.SubprocessError):
                    # Graceful `adb emu kill` failed or timed out; the direct
                    # process terminate/kill below still stops the emulator.
                    pass

            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                except Exception as e:
                    logger.error(f"Error terminating emulator process {pid}: {e}")

            self._current_state = EmulatorLaunchState(
                avd_name=avd,
                status=EmulatorLaunchStage.STOPPED,
                stage_message="Emulator process stopped by user.",
                logs=list(self._log_buffer),
            )
            return {"success": True, "message": f"Emulator '{avd}' stopped"}

    def dismiss(self) -> dict[str, Any]:
        """Reset emulator launch state back to idle."""
        self._current_state = EmulatorLaunchState()
        self._log_buffer.clear()
        return {"success": True, "message": "State reset"}


# Global singleton instance
emulator_manager = EmulatorManager()
