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

"""Tests for the guest-side network setup applied when an emulator finishes booting.

The stock guest probes ``www.google.com`` for connectivity; on a network that
cannot reach Google both probes time out, Android reports PARTIAL_CONNECTIVITY
instead of VALIDATED, and apps on the device claim there is no network. The
setup must repoint those probes at a reachable host (emulators only) and fail
soft.
"""

import importlib

import pytest

#: `artemis.core.diagnostics` exports a *singleton* called `emulator_manager`,
#: which shadows the submodule of the same name — resolve the module itself.
emulator_manager = importlib.import_module("artemis.core.diagnostics.emulator_manager")


class _FakeProc:
    def __init__(self, rc: int = 0, out: bytes = b"ok"):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, b""


@pytest.fixture()
def commands(monkeypatch):
    """Record the adb command lines instead of executing them."""
    recorded: list[tuple[str, ...]] = []

    async def _fake_run(adb_path, serial, *args, timeout=8.0):
        recorded.append((serial, *args))
        return True

    monkeypatch.setattr(emulator_manager, "_run_adb", _fake_run)
    monkeypatch.setattr(emulator_manager, "_REVALIDATE_PAUSE_SECONDS", 0.0)
    return recorded


@pytest.mark.asyncio
async def test_physical_phone_is_left_alone(commands):
    """A real phone has its own validated network: never rewrite its settings."""
    applied = await emulator_manager.configure_emulator_network("/sdk/adb", "D123084100AC")

    assert applied == []
    assert commands == []


@pytest.mark.asyncio
async def test_emulator_probe_urls_point_at_a_reachable_host(commands):
    applied = await emulator_manager.configure_emulator_network("/sdk/adb", "emulator-5554")

    assert (
        "emulator-5554",
        "shell",
        "settings",
        "put",
        "global",
        "captive_portal_https_url",
        emulator_manager._CAPTIVE_PORTAL_HTTPS_URL,
    ) in commands
    assert (
        "emulator-5554",
        "shell",
        "settings",
        "put",
        "global",
        "captive_portal_http_url",
        emulator_manager._CAPTIVE_PORTAL_HTTP_URL,
    ) in commands
    assert "www.google.com" not in "".join(commands.__repr__())
    assert "captive_portal_https_url" in applied
    assert "captive_portal_http_url" in applied


@pytest.mark.asyncio
async def test_emulator_ipv6_is_switched_off_after_root(commands):
    await emulator_manager.configure_emulator_network("/sdk/adb", "emulator-5554")

    sysctls = [c for c in commands if "sysctl" in c]
    assert sysctls == [
        ("emulator-5554", "shell", "sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=1"),
        ("emulator-5554", "shell", "sysctl", "-w", "net.ipv6.conf.default.disable_ipv6=1"),
        ("emulator-5554", "shell", "sysctl", "-w", "net.ipv6.conf.eth0.disable_ipv6=1"),
    ]
    # sysctl needs root, so `adb root` must come first.
    assert commands.index(("emulator-5554", "root")) < commands.index(sysctls[0])


@pytest.mark.asyncio
async def test_validation_is_forced_again_after_the_settings_change(commands):
    """Android does not re-read the probe URLs; the radio bounce re-probes."""
    applied = await emulator_manager.configure_emulator_network("/sdk/adb", "emulator-5554")

    assert ("emulator-5554", "shell", "svc", "data", "disable") in commands
    assert ("emulator-5554", "shell", "svc", "data", "enable") in commands
    assert commands.index(("emulator-5554", "shell", "svc", "data", "disable")) < commands.index(
        ("emulator-5554", "shell", "svc", "data", "enable")
    )
    assert "revalidated" in applied


@pytest.mark.asyncio
async def test_every_step_failing_still_returns(monkeypatch):
    """A locked-down image loses a nicety, never the boot or an exception."""
    calls = []

    async def _deny(*args, **kwargs):
        calls.append(args)
        return False

    monkeypatch.setattr(emulator_manager, "_run_adb", _deny)
    monkeypatch.setattr(emulator_manager, "_REVALIDATE_PAUSE_SECONDS", 0.0)

    applied = await emulator_manager.configure_emulator_network("/sdk/adb", "emulator-5554")

    assert applied == []
    assert calls  # it did try


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proc", "expected"),
    [(_FakeProc(rc=0), True), (_FakeProc(rc=1), False)],
)
async def test_run_adb_reports_exit_status(monkeypatch, proc, expected):
    async def _spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(emulator_manager.asyncio, "create_subprocess_exec", _spawn)

    assert await emulator_manager._run_adb("/sdk/adb", "emulator-5554", "shell", "echo") is expected


@pytest.mark.asyncio
async def test_run_adb_swallows_spawn_errors(monkeypatch):
    async def _spawn(*args, **kwargs):
        raise OSError("adb not found")

    monkeypatch.setattr(emulator_manager.asyncio, "create_subprocess_exec", _spawn)

    assert await emulator_manager._run_adb("/sdk/adb", "emulator-5554", "shell", "echo") is False
