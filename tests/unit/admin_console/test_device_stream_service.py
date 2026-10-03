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

"""Tests for the task-following live screen stream.

The stream must mirror *the watched task's* device and nothing else: several
tasks may run on different devices at once, so picking "the first attached
device" (the old behaviour) put the live view on someone else's phone. When
there is no device information the endpoints report an error instead of
guessing.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import stream as stream_router
from apps.admin_console.services.device_stream_service import (
    DeviceStreamService,
    StreamDeviceError,
    StreamDeviceUnavailable,
)
from artemis.runtime import trace_store

SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"


def _async_result(value):
    """A coroutine-returning stand-in for an async helper."""

    async def _inner():
        return value

    return _inner()


@pytest.fixture()
def clean_state(monkeypatch):
    """Empty task records: no active run, no queue rows, nothing on disk."""
    monkeypatch.setattr(state, "active_session_id", None)
    monkeypatch.setattr(state, "active_runs", {})
    monkeypatch.setattr(state, "queue_items", [])
    monkeypatch.setattr(trace_store, "read_status", lambda trace_id: None)
    monkeypatch.setattr(session_repo, "get_session_by_id", lambda session_id: None)
    monkeypatch.setattr(session_repo, "get_running_session_id", lambda: None)
    return DeviceStreamService()


def _task_in_active_run(monkeypatch, session_id: str, serial: str) -> None:
    monkeypatch.setattr(
        state,
        "active_runs",
        {session_id: {"device_id": serial, "goal": "g", "profile": "flash"}},
    )


# ---------------------------------------------------------------- resolution


@pytest.mark.asyncio
async def test_follows_the_requested_task_from_the_running_run(clean_state, monkeypatch):
    _task_in_active_run(monkeypatch, SESSION_A, "emulator-5554")

    device = await clean_state.resolve_stream_device(SESSION_A)

    assert device.session_id == SESSION_A
    assert device.serial == "emulator-5554"
    assert device.source == "active_run"


@pytest.mark.asyncio
async def test_follows_the_requested_task_from_status_json(clean_state, monkeypatch):
    monkeypatch.setattr(
        trace_store,
        "read_status",
        lambda trace_id: (
            {"status": "running", "device_serial": "emulator-5554"}
            if trace_id == SESSION_A
            else None
        ),
    )

    device = await clean_state.resolve_stream_device(SESSION_A)

    assert device.serial == "emulator-5554"
    assert device.source == "status"


@pytest.mark.asyncio
async def test_follows_a_task_launched_by_another_process_via_the_db(clean_state, monkeypatch):
    """MCP/CLI tasks run outside this server; the sessions table is the record."""
    monkeypatch.setattr(
        session_repo,
        "get_session_by_id",
        lambda session_id: {
            "session_id": session_id,
            "status": "running",
            "device_info": '{"device_id": "D123084100AC"}',
        },
    )

    device = await clean_state.resolve_stream_device(SESSION_A)

    assert device.serial == "D123084100AC"
    assert device.source == "session_db"


@pytest.mark.asyncio
async def test_without_an_explicit_session_it_follows_the_active_task(clean_state, monkeypatch):
    monkeypatch.setattr(state, "active_session_id", SESSION_B)
    _task_in_active_run(monkeypatch, SESSION_B, "emulator-5556")

    device = await clean_state.resolve_stream_device(None)

    assert device.session_id == SESSION_B
    assert device.serial == "emulator-5556"


@pytest.mark.asyncio
async def test_without_an_in_process_run_it_follows_the_db_running_task(clean_state, monkeypatch):
    monkeypatch.setattr(session_repo, "get_running_session_id", lambda: SESSION_A)
    monkeypatch.setattr(
        session_repo,
        "get_session_by_id",
        lambda session_id: {"device_info": '{"device_id": "emulator-5554"}'},
    )

    device = await clean_state.resolve_stream_device(None)

    assert device.session_id == SESSION_A
    assert device.serial == "emulator-5554"


@pytest.mark.asyncio
async def test_task_switch_follows_the_new_task(clean_state, monkeypatch):
    _task_in_active_run(monkeypatch, SESSION_A, "emulator-5554")
    assert (await clean_state.resolve_stream_device(SESSION_A)).serial == "emulator-5554"

    # The viewer switches to another task running on another device.
    monkeypatch.setattr(
        trace_store,
        "read_status",
        lambda trace_id: {"device_serial": "D123084100AC"} if trace_id == SESSION_B else None,
    )

    assert (await clean_state.resolve_stream_device(SESSION_B)).serial == "D123084100AC"


# ------------------------------------------------------------ no guessing


@pytest.mark.asyncio
async def test_errors_when_no_task_is_running(clean_state):
    with pytest.raises(StreamDeviceError, match="No running task to follow"):
        await clean_state.resolve_stream_device(None)


@pytest.mark.asyncio
async def test_errors_for_an_unknown_task(clean_state):
    with pytest.raises(StreamDeviceError, match="No task .* no record exists"):
        await clean_state.resolve_stream_device(SESSION_A)


@pytest.mark.asyncio
async def test_errors_when_the_task_records_no_device(clean_state, monkeypatch):
    monkeypatch.setattr(trace_store, "read_status", lambda trace_id: {"status": "running"})
    monkeypatch.setattr(session_repo, "get_session_by_id", lambda session_id: {"device_info": ""})

    with pytest.raises(StreamDeviceError, match="records no device"):
        await clean_state.resolve_stream_device(SESSION_A)


@pytest.mark.asyncio
async def test_resolution_never_asks_adb_which_device_to_use(clean_state, monkeypatch):
    """Attached devices are irrelevant: only the task decides what is mirrored."""
    _task_in_active_run(monkeypatch, SESSION_A, "emulator-5554")

    def _boom():
        raise AssertionError("resolution must not pick a device from adb")

    monkeypatch.setattr(clean_state, "list_adb_devices", _boom)

    device = await clean_state.resolve_stream_device(SESSION_A)

    assert device.serial == "emulator-5554"


@pytest.mark.asyncio
async def test_validate_online_rejects_a_disconnected_task_device(clean_state, monkeypatch):
    monkeypatch.setattr(clean_state, "list_adb_devices", lambda: _async_result({}))

    with pytest.raises(StreamDeviceUnavailable, match="emulator-5554 is not attached"):
        await clean_state.validate_online("emulator-5554")


@pytest.mark.asyncio
async def test_validate_online_rejects_an_offline_task_device(clean_state, monkeypatch):
    monkeypatch.setattr(
        clean_state, "list_adb_devices", lambda: _async_result({"emulator-5554": "offline"})
    )

    with pytest.raises(StreamDeviceUnavailable, match="is offline"):
        await clean_state.validate_online("emulator-5554")


# ---------------------------------------------------------------- endpoints


@pytest.mark.asyncio
async def test_device_state_reports_the_watched_task_device(clean_state, monkeypatch):
    monkeypatch.setattr(state, "active_session_id", SESSION_A)
    _task_in_active_run(monkeypatch, SESSION_A, "emulator-5554")
    monkeypatch.setattr(
        stream_router.device_stream_service,
        "list_adb_devices",
        lambda: _async_result({"emulator-5554": "device"}),
    )
    _clear_stream_cache()

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://localhost") as ac:
        res = await ac.get("/api/stream/device-state", params={"session_id": SESSION_A})

    assert res.status_code == 200
    assert res.json() == {
        "connected": True,
        "serial": "emulator-5554",
        "session_id": SESSION_A,
        "source": "active_run",
        "error": None,
        "live_stream_url": "/api/stream/device-live",
    }


@pytest.mark.asyncio
async def test_device_state_explains_a_missing_device(clean_state, monkeypatch):
    monkeypatch.setattr(state, "active_session_id", None)
    _clear_stream_cache()

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://localhost") as ac:
        res = await ac.get("/api/stream/device-state")

    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is False
    assert body["live_stream_url"] is None
    assert "No running task to follow" in body["error"]


@pytest.mark.asyncio
async def test_device_live_errors_when_there_is_no_device_information(clean_state, monkeypatch):
    monkeypatch.setattr(state, "active_session_id", None)
    _clear_stream_cache()

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://localhost") as ac:
        res = await ac.get("/api/stream/device-live")

    assert res.status_code == 409
    assert "No running task to follow" in res.json()["detail"]


@pytest.mark.asyncio
async def test_device_live_errors_when_the_task_device_is_offline(clean_state, monkeypatch):
    monkeypatch.setattr(state, "active_session_id", SESSION_A)
    _task_in_active_run(monkeypatch, SESSION_A, "emulator-5554")
    monkeypatch.setattr(
        stream_router.device_stream_service,
        "list_adb_devices",
        lambda: _async_result({"emulator-5554": "offline"}),
    )
    _clear_stream_cache()

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://localhost") as ac:
        res = await ac.get("/api/stream/device-live", params={"session_id": SESSION_A})

    assert res.status_code == 503
    assert "offline" in res.json()["detail"]


def _clear_stream_cache():
    stream_router.device_stream_service._device_cache = None


def _app():
    from apps.admin_console.server import app

    return app
