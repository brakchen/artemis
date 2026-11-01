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

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import time
from types import SimpleNamespace

from filelock import FileLock
import pytest

from artemis.runtime import device_lock as device_lock_module
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _name: tmp_path,
    )


def test_device_lock_blocks_a_second_process_owner():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")

    first.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="first task"):
            second.acquire(blocking=False)
    finally:
        first.release()


def test_device_lock_recovers_stale_owner(monkeypatch):
    lock = DeviceExecutionLock("emulator-5554", "replacement task")
    lock.path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "emulator-5554",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(lambda _owner: False))

    lock.acquire()
    try:
        assert lock.path.exists()
    finally:
        lock.release()

    assert not lock.path.exists()


def test_same_serial_on_different_adb_endpoints_has_independent_locks():
    first = DeviceExecutionLock(
        "emulator-5554",
        "first endpoint",
        lock_scope="tcp:127.0.0.1:5037",
    )
    second = DeviceExecutionLock(
        "emulator-5554",
        "second endpoint",
        lock_scope="tcp:127.0.0.1:5038",
    )

    first.acquire()
    second.acquire()
    try:
        assert first.path != second.path
        owners = DeviceExecutionLock.get_active_owners()
        assert len(owners) == 2
        assert {owner.lock_scope for owner in owners.values()} == {
            "tcp:127.0.0.1:5037",
            "tcp:127.0.0.1:5038",
        }
    finally:
        second.release()
        first.release()


def test_device_lock_release_does_not_remove_replacement_owner():
    lock = DeviceExecutionLock("emulator-5554", "original task")
    lock.acquire()
    replacement = json.loads(lock.path.read_text(encoding="utf-8"))
    replacement["token"] = "replacement-token"
    lock.path.write_text(json.dumps(replacement), encoding="utf-8")

    lock.release()

    assert lock.path.exists()


def test_cleanup_stale_locks_keeps_live_owner(monkeypatch):
    live = DeviceExecutionLock("emulator-5554", "live task")
    live.acquire()
    stale_path = live.path.parent / "stale-device.lock"
    stale_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "physical-device",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    original_owner_is_alive = DeviceExecutionLock._owner_is_alive

    def owner_is_alive(owner):
        if owner.pid == 999999:
            return False
        return original_owner_is_alive(owner)

    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(owner_is_alive))
    try:
        assert DeviceExecutionLock.cleanup_stale_locks() == 1
        assert live.path.exists()
        assert not stale_path.exists()
    finally:
        live.release()


def test_device_lock_waits_and_runs_next_owner_in_fifo_order():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")
    acquired = threading.Event()

    first.acquire()

    def acquire_second():
        second.acquire()
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not acquired.is_set()

    first.release()
    assert acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


def test_submission_reservations_preserve_order_before_workers_start():
    first_ticket = DeviceExecutionLock.reserve("first submitted task")
    second_ticket = DeviceExecutionLock.reserve("second submitted task")
    second = DeviceExecutionLock("emulator-5554", "second task", second_ticket)
    second_acquired = threading.Event()

    def acquire_second():
        second.acquire(timeout=2.0)
        second_acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not second_acquired.is_set()

    first = DeviceExecutionLock("emulator-5554", "first task", first_ticket)
    first.acquire(timeout=2.0)
    first.release()

    assert second_acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


@pytest.mark.parametrize("clock_values", [(1000, 1000, 1000), (1000, 999, 998)])
@pytest.mark.parametrize("concurrency_mode", ["global", "per_device"])
def test_reservations_follow_admission_with_stalled_or_rewound_clock(
    monkeypatch, tmp_path, clock_values, concurrency_mode
):
    clocks = iter(clock_values)
    tokens = iter(SimpleNamespace(hex=value * 32) for value in ("f", "8", "1"))
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(device_lock_module, "time", SimpleNamespace(**vars(time)))
        clock_patch.setattr(device_lock_module.time, "time_ns", lambda: next(clocks))
        clock_patch.setattr(device_lock_module.uuid, "uuid4", lambda: next(tokens))
        issued = [DeviceExecutionLock.reserve("task-0", "device-alpha")]
        # Sequential completion is ordered across the main and worker threads.
        with ThreadPoolExecutor(max_workers=2) as pool:
            issued.extend(
                pool.submit(DeviceExecutionLock.reserve, f"task-{index}", "device-alpha").result()
                for index in range(1, 3)
            )
    paths = sorted((tmp_path / "artemis-global-device.queue").glob("*.wait"))
    assert [path.stem.partition("-")[2] for path in paths] == issued
    assert [int(path.stem.partition("-")[0]) for path in paths] == [1000, 1001, 1002]

    first = DeviceExecutionLock("device-alpha", queue_ticket=issued[0])
    younger = DeviceExecutionLock(
        "device-alpha", queue_ticket=issued[1], concurrency_mode=concurrency_mode
    )
    try:
        with pytest.raises(DeviceBusyError, match="earlier queued task"):
            younger.acquire(blocking=False)
        first.acquire(blocking=False)
    finally:
        first.release()
        for ticket in issued:
            DeviceExecutionLock.cancel_reservation(ticket)


@pytest.mark.parametrize("clock_values", [(1000, 1000, 1000), (1000, 999, 998)])
def test_reservations_follow_completed_admission_across_real_processes(tmp_path, clock_values):
    script = """
import json
import hashlib
from pathlib import Path
import sys
import site
from types import ModuleType, SimpleNamespace
site.addsitedir(sys.argv[4])
# Load the real queue and process probe without unrelated public SDK startup.
root = Path(sys.argv[5])
for name, relative in [('artemis', 'artemis'), ('artemis.config', 'artemis/config'), ('artemis.runtime', 'artemis/runtime')]:
    package = ModuleType(name)
    package.__path__ = [str(root / relative)]
    sys.modules[name] = package
paths = ModuleType('artemis.config.paths')
paths.get_temp_dir = lambda _name: Path(sys.argv[1])
sys.modules[paths.__name__] = paths
from artemis.runtime import device_lock as module
module.get_temp_dir = lambda _name: Path(sys.argv[1])
module.time = SimpleNamespace(time_ns=lambda: int(sys.argv[2]))
module.uuid.uuid4 = lambda: SimpleNamespace(hex=sys.argv[3])
ticket = module.DeviceExecutionLock.reserve('child admission', device_id='device-alpha')
source = Path(module.DeviceExecutionLock.reserve.__func__.__code__.co_filename)
print(json.dumps({'ticket': ticket, 'pid': __import__('os').getpid(), 'owner_source': str(source), 'owner_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}), flush=True)
"""
    children = []
    issued = []
    child_env = {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "ARTEMIS_APP_DIR": str(tmp_path / "child-app"),
        "ANTIGRAVITY_APP_DIR": str(tmp_path / "child-app"),
        "LOCALAPPDATA": str(tmp_path / "child-localapp"),
    }
    try:
        for clock_value, token in zip(clock_values, ("f" * 32, "8" * 32, "1" * 32)):
            child = subprocess.Popen(
                [
                    sys._base_executable,
                    "-S",
                    "-c",
                    script,
                    str(tmp_path),
                    str(clock_value),
                    token,
                    sysconfig.get_path("purelib"),
                    str(Path(__file__).resolve().parents[3]),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            children.append(child)
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            reply = json.loads(stdout)
            assert reply["pid"] == child.pid
            print(json.dumps({"popen_pid": child.pid, "returncode": child.returncode, **reply}))
            issued.append(reply["ticket"])
        paths = sorted((tmp_path / "artemis-global-device.queue").glob("*.wait"))
        print(
            json.dumps(
                {
                    "clock_values": clock_values,
                    "issued": issued,
                    "sorted_tokens": [path.stem.partition("-")[2] for path in paths],
                }
            )
        )
        assert [path.stem.partition("-")[2] for path in paths] == issued
        assert [int(path.stem.partition("-")[0]) for path in paths] == [1000, 1001, 1002]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()


def test_native_admission_lock_recovers_after_its_process_exits(tmp_path):
    queue_dir = tmp_path / "artemis-global-device.queue"
    queue_dir.mkdir()
    lock_path = queue_dir / ".admission.lock"
    script = """
import os
import sys
import site
site.addsitedir(sys.argv[2])
from filelock import FileLock
lock = FileLock(sys.argv[1], preserve_lock_file=True)
lock.acquire()
print('locked', flush=True)
os._exit(0)
"""
    child = subprocess.Popen(
        [sys._base_executable, "-S", "-c", script, str(lock_path), sysconfig.get_path("purelib")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
    )
    try:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "locked"
        with FileLock(lock_path, timeout=1, preserve_lock_file=True):
            assert DeviceExecutionLock.cleanup_stale_locks() == 0
            assert DeviceExecutionLock.get_active_owners() == {}
            assert list(queue_dir.glob("*.wait")) == []
        ticket = DeviceExecutionLock.reserve("after crashed admission")
        assert DeviceExecutionLock.cancel_reservation(ticket)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


@pytest.fixture
def native_admission_holder(tmp_path):
    @contextmanager
    def hold():
        queue_dir = tmp_path / "artemis-global-device.queue"
        queue_dir.mkdir(exist_ok=True)
        script = """
import os, site, sys
site.addsitedir(sys.argv[2])
from filelock import FileLock
with FileLock(sys.argv[1], timeout=2, preserve_lock_file=True):
    print(os.getpid(), flush=True)
    sys.stdin.readline()
"""
        child = subprocess.Popen(
            [
                sys._base_executable,
                "-S",
                "-c",
                script,
                str(queue_dir / ".admission.lock"),
                sysconfig.get_path("purelib"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PYTHON_DOTENV_DISABLED": "1",
                "ARTEMIS_APP_DIR": str(tmp_path / "holder-app"),
                "LOCALAPPDATA": str(tmp_path / "holder-localapp"),
            },
        )

        def release():
            if child.poll() is None:
                try:
                    stdout, stderr = child.communicate("release\n", timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    raise
                assert child.returncode == 0, (stdout, stderr)

        try:
            assert child.stdout.readline().strip() == str(child.pid)
            yield child, release
        finally:
            release()
            child.communicate()

    return hold


@pytest.mark.parametrize("control", ["nonblocking", "timeout", "preset_cancel", "during_cancel"])
def test_acquire_admission_obeys_wait_controls_before_holder_release(
    native_admission_holder, monkeypatch, control
):
    unrelated = DeviceExecutionLock.reserve("unrelated", device_id="device-gamma")
    unrelated_path = DeviceExecutionLock._reservation_paths(unrelated)[0]
    unrelated_bytes = unrelated_path.read_bytes()
    contender = DeviceExecutionLock("device-alpha", control)
    cancel = threading.Event()
    if control == "preset_cancel":
        cancel.set()
    kwargs = {"poll_interval": 0.01}
    if control == "nonblocking":
        kwargs["blocking"] = False
    elif control == "timeout":
        kwargs["timeout"] = 0.1
    else:
        kwargs["cancel_event"] = cancel
    attempted = threading.Event()
    finished = threading.Event()
    outcomes = []
    original_acquire = FileLock._acquire

    def observe(lock):
        original_acquire(lock)
        if threading.current_thread() is worker and not lock.is_locked:
            attempted.set()

    def acquire():
        started = time.perf_counter()
        try:
            contender.acquire(**kwargs)
        except BaseException as exc:
            outcomes.append(exc)
        else:
            outcomes.append("acquired")
        finally:
            contender.release()
            outcomes.append(time.perf_counter() - started)
            finished.set()

    worker = threading.Thread(target=acquire)
    monkeypatch.setattr(FileLock, "_acquire", observe)
    with native_admission_holder() as (holder, release):
        worker.start()
        try:
            if control == "during_cancel":
                assert attempted.wait(timeout=1)
                cancel.set()
            assert finished.wait(timeout=0.4), (
                "acquire ignored controls while admission remained held"
            )
            assert holder.poll() is None
            assert isinstance(outcomes[0], DeviceBusyError)
            if "cancel" in control:
                assert "cancelled" in str(outcomes[0])
            assert list(contender.queue_dir.glob("*.wait")) == [unrelated_path]
            assert list(contender.queue_dir.glob("*.tmp")) == []
            assert not contender.path.exists()
            assert not contender._acquired
            assert contender.queue_ticket is None
            assert unrelated_path.read_bytes() == unrelated_bytes
        finally:
            release()
            worker.join(timeout=2)
            print(
                {
                    "control": control,
                    "holder_pid": holder.pid,
                    "holder_returncode": holder.returncode,
                    "outcome": str(outcomes[0]) if outcomes else None,
                    "elapsed": outcomes[-1] if outcomes else None,
                    "worker_reaped": not worker.is_alive(),
                }
            )
    assert not worker.is_alive()
    with FileLock(contender.queue_dir / ".admission.lock", timeout=0, preserve_lock_file=True):
        assert not contender.path.exists()
    DeviceExecutionLock.cancel_reservation(unrelated)
    successor = DeviceExecutionLock("device-alpha", "after controlled admission")
    successor.acquire(blocking=False)
    successor.release()


@pytest.mark.parametrize("waiting_for", ["device", "capacity"])
@pytest.mark.parametrize("poll_interval", [0.01, 0.5])
def test_acquire_timeout_budget_starts_before_claim_and_is_not_reset(
    monkeypatch, waiting_for, poll_interval
):
    owner = DeviceExecutionLock("device-alpha", "active task")
    owner.acquire()
    contender = DeviceExecutionLock(
        "device-alpha" if waiting_for == "device" else "device-beta",
        max_concurrency=1 if waiting_for == "capacity" else None,
    )
    clock = [10.0]
    original_claim = DeviceExecutionLock._claim_or_create_queue_ticket

    def delayed_claim(lock, **kwargs):
        clock[0] += 0.08
        return original_claim(lock, **kwargs)

    fake_time = SimpleNamespace(**vars(time))
    fake_time.monotonic = lambda: clock[0]
    fake_time.sleep = lambda delay: clock.__setitem__(0, clock[0] + delay)
    monkeypatch.setattr(device_lock_module, "time", fake_time)
    monkeypatch.setattr(DeviceExecutionLock, "_claim_or_create_queue_ticket", delayed_claim)
    try:
        with pytest.raises(DeviceBusyError, match="Timed out"):
            contender.acquire(timeout=0.1, poll_interval=poll_interval)
        assert 10.1 <= clock[0] <= 10.12
        assert not contender.path.exists() or contender.path == owner.path
        assert owner.path.exists()
        assert list(contender.queue_dir.glob("*.wait")) == []
    finally:
        contender.release()
        owner.release()


def test_acquire_cancellation_during_ticket_write_does_not_publish(monkeypatch):
    cancel = threading.Event()
    original_write = os.write
    original_replace = os.replace
    published = []

    def write_and_cancel(fd, payload):
        written = original_write(fd, payload)
        cancel.set()
        return written

    def observe_publish(src, dst):
        published.append(dst)
        return original_replace(src, dst)

    monkeypatch.setattr(os, "write", write_and_cancel)
    monkeypatch.setattr(os, "replace", observe_publish)
    contender = DeviceExecutionLock("device-alpha")
    with pytest.raises(DeviceBusyError, match="cancelled"):
        contender.acquire(cancel_event=cancel)
    assert published == []
    assert list(contender.queue_dir.glob("*.wait")) == []
    assert list(contender.queue_dir.glob("*.tmp")) == []
    assert not contender.path.exists()


def test_pre_cancelled_delegated_ticket_is_not_claimed(monkeypatch, native_admission_holder):
    ticket = DeviceExecutionLock.reserve("delegated", device_id="device-alpha")
    unrelated = DeviceExecutionLock.reserve("unrelated", device_id="device-gamma")
    other_path = DeviceExecutionLock._reservation_paths(unrelated)[0]
    other_bytes = other_path.read_bytes()
    contender = DeviceExecutionLock("device-alpha", queue_ticket=ticket)
    cancel = threading.Event()
    cancel.set()
    claims = []
    original_claim = DeviceExecutionLock._atomic_write_json

    def observe_claim(cls, path, payload):
        claims.append(path)
        return original_claim(path, payload)

    monkeypatch.setattr(DeviceExecutionLock, "_atomic_write_json", classmethod(observe_claim))
    with native_admission_holder():
        with pytest.raises(DeviceBusyError, match="cancelled"):
            contender.acquire(cancel_event=cancel)
        assert claims == []
        assert DeviceExecutionLock._reservation_paths(ticket) == []
        assert other_path.read_bytes() == other_bytes
        assert not contender.path.exists()
    DeviceExecutionLock.cancel_reservation(unrelated)


@pytest.mark.parametrize("control", ["deadline", "cancellation"])
def test_late_native_admission_is_rejected_before_publication(monkeypatch, control):
    cancel = threading.Event()
    clock = [10.0]
    original_acquire = FileLock.acquire
    original_replace = os.replace
    published = []
    contender = DeviceExecutionLock("device-alpha")

    def late_acquire(lock, *args, **kwargs):
        admitted = original_acquire(lock, *args, **kwargs)
        if control == "deadline":
            clock[0] = 10.2
        else:
            cancel.set()
        return admitted

    def observe_publish(src, dst):
        published.append(dst)
        return original_replace(src, dst)

    with monkeypatch.context() as controls:
        controls.setattr(device_lock_module, "time", SimpleNamespace(**vars(time)))
        controls.setattr(device_lock_module.time, "monotonic", lambda: clock[0])
        controls.setattr(FileLock, "acquire", late_acquire)
        controls.setattr(os, "replace", observe_publish)
        with pytest.raises(
            DeviceBusyError, match="Timed out" if control == "deadline" else "cancelled"
        ):
            contender.acquire(timeout=0.1, cancel_event=cancel)
    assert published == []
    assert list(contender.queue_dir.glob("*.wait")) == []
    assert list(contender.queue_dir.glob("*.tmp")) == []
    assert not contender.path.exists()
    with FileLock(contender.queue_dir / ".admission.lock", timeout=0, preserve_lock_file=True):
        pass


@pytest.mark.parametrize("phase", ["before_claim", "after_claim"])
def test_delegated_cancel_cleanup_preserves_original_object_and_notes(monkeypatch, phase):
    ticket = DeviceExecutionLock.reserve("delegated", device_id="device-alpha")
    unrelated = DeviceExecutionLock.reserve("unrelated", device_id="device-gamma")
    path = DeviceExecutionLock._reservation_paths(ticket)[0]
    other_path = DeviceExecutionLock._reservation_paths(unrelated)[0]
    other_bytes = other_path.read_bytes()
    contender = DeviceExecutionLock("device-alpha", queue_ticket=ticket)
    failure = asyncio.CancelledError("original delegated cancellation")
    interrupted = [phase == "before_claim"]
    original_write = DeviceExecutionLock._atomic_write_json
    original_unlink = Path.unlink

    class Interrupted:
        def is_set(self):
            if interrupted[0]:
                raise failure
            return False

    def claim_and_interrupt(cls, target, payload):
        result = original_write(target, payload)
        interrupted[0] = True
        return result

    def deny_own_cleanup(target, *args, **kwargs):
        if target == path:
            raise PermissionError("delegated cleanup refused")
        return original_unlink(target, *args, **kwargs)

    with monkeypatch.context() as controls:
        controls.setattr(
            DeviceExecutionLock, "_atomic_write_json", classmethod(claim_and_interrupt)
        )
        controls.setattr(Path, "unlink", deny_own_cleanup)
        with pytest.raises(asyncio.CancelledError) as raised:
            contender.acquire(cancel_event=Interrupted())
        assert raised.value is failure
        assert raised.value.__notes__ == [
            "Could not cancel the failed Artemis queue reservation; its ticket remains pending."
        ]
        assert path.exists()
        assert other_path.read_bytes() == other_bytes
        assert not contender.path.exists()
    assert DeviceExecutionLock.cancel_reservation(ticket)
    assert DeviceExecutionLock.cancel_reservation(unrelated)


@pytest.mark.parametrize("timeout", [None, 0.0, 0.1])
@pytest.mark.parametrize("blocking", [False, True])
def test_uncontended_admission_keeps_immediate_acquire_semantics(timeout, blocking):
    lock = DeviceExecutionLock("device-alpha")
    lock.acquire(timeout=timeout, blocking=blocking)
    try:
        assert lock._acquired
        assert lock.path.exists()
        assert list(lock.queue_dir.glob("*.wait")) == []
    finally:
        lock.release()


@pytest.mark.parametrize("body_failure", [None, "cancelled", "ordinary"])
@pytest.mark.parametrize("cleanup_denied", [False, True])
def test_native_admission_release_errors_preserve_publication_and_primary_failure(
    monkeypatch, body_failure, cleanup_denied
):
    active = DeviceExecutionLock("device-active", "unrelated active owner")
    active.acquire()
    active_bytes = active.path.read_bytes()
    unrelated = DeviceExecutionLock.reserve("unrelated queued ticket", device_id="device-gamma")
    unrelated_path = DeviceExecutionLock._reservation_paths(unrelated)[0]
    unrelated_bytes = unrelated_path.read_bytes()
    contender = DeviceExecutionLock("device-alpha", "native release reporting failure")
    primary = (
        asyncio.CancelledError("original body cancellation")
        if body_failure == "cancelled"
        else RuntimeError("original body failure")
    )
    primary_cause = ValueError("original body cause")
    secondary = OSError("native close report after actual unlock and FD close")
    secondary_cause = ValueError("original release cause")
    original_close = FileLock._close_released_fd
    original_replace = os.replace
    original_unlink = Path.unlink
    released_fds = []
    published = []

    def native_close_then_report(lock, fd, **kwargs):
        original_close(lock, fd, **kwargs)
        if Path(lock.lock_file).name == ".admission.lock":
            with pytest.raises(OSError):
                os.fstat(fd)
            released_fds.append(fd)
            raise secondary from secondary_cause

    def abort_body(*_args):
        raise primary from primary_cause

    def observe_publish(src, dst):
        result = original_replace(src, dst)
        published.append(dst)
        return result

    def deny_own_cleanup(path, *args, **kwargs):
        if (
            cleanup_denied
            and path.parent == contender.queue_dir
            and (path in published or path.suffix == ".tmp")
        ):
            raise PermissionError("owned reservation cleanup refused")
        return original_unlink(path, *args, **kwargs)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(FileLock, "_close_released_fd", native_close_then_report)
            faults.setattr(os, "replace", observe_publish)
            faults.setattr(Path, "unlink", deny_own_cleanup)
            if body_failure is not None:
                faults.setattr(os, "write", abort_body)
            with pytest.raises(BaseException) as raised:
                contender.acquire(blocking=False)
        # The injected error reports after a real native unlock and descriptor
        # close; the real native gate remains immediately acquirable afterward.
        assert released_fds
        with FileLock(contender.queue_dir / ".admission.lock", timeout=0, preserve_lock_file=True):
            pass
        remaining_waits = [
            path for path in contender.queue_dir.glob("*.wait") if path != unrelated_path
        ]
        remaining_temps = list(contender.queue_dir.glob("*.tmp"))
        notes = getattr(raised.value, "__notes__", [])
        print(
            {
                "body_failure": body_failure,
                "cleanup_denied": cleanup_denied,
                "raised": type(raised.value).__name__,
                "same_primary": raised.value is primary,
                "same_release_error": raised.value is secondary,
                "native_fds_closed": released_fds,
                "native_lock_reacquired": True,
                "remaining_waits": [path.name for path in remaining_waits],
                "remaining_temps": [path.name for path in remaining_temps],
                "notes": notes,
            }
        )
        assert unrelated_path.read_bytes() == unrelated_bytes
        assert active.path.read_bytes() == active_bytes
        assert not contender.path.exists()
        assert contender.queue_ticket is None
        assert contender._queue_path is None
        if body_failure is None:
            assert raised.value is secondary
            assert raised.value.__cause__ is secondary_cause
            assert len(published) == 1
            if cleanup_denied:
                assert remaining_waits == published
                assert any("published Artemis queue reservation" in note for note in notes)
            else:
                assert remaining_waits == []
        else:
            assert raised.value is primary
            assert raised.value.__cause__ is primary_cause
            assert published == []
            assert any(str(secondary) in note for note in notes)
            assert remaining_waits == []
            if cleanup_denied:
                assert len(remaining_temps) == 1
                assert any(
                    "unpublished Artemis queue reservation temp file" in note for note in notes
                )
        if not cleanup_denied:
            assert remaining_temps == []
        # A permanent injected delete denial is explicitly reported above;
        # once that denial ends, clean our own leftovers before probing progress.
        for path in remaining_waits:
            assert DeviceExecutionLock.cancel_reservation(path.stem.partition("-")[2])
        for path in remaining_temps:
            path.unlink()
        successor = DeviceExecutionLock("device-alpha", "same-device successor")
        successor.acquire(blocking=False)
        successor.release()
        assert unrelated_path.read_bytes() == unrelated_bytes
        assert active.path.read_bytes() == active_bytes
    finally:
        for path in contender.queue_dir.glob("*.wait"):
            if path != unrelated_path:
                DeviceExecutionLock.cancel_reservation(path.stem.partition("-")[2])
        for path in contender.queue_dir.glob("*.tmp"):
            path.unlink()
        DeviceExecutionLock.cancel_reservation(unrelated)
        active.release()


@pytest.mark.parametrize("phase", ["lock", "scan", "open", "write", "close", "publish"])
def test_failed_admission_leaves_no_published_ticket(monkeypatch, tmp_path, phase):
    failure = PermissionError(f"{phase} denied")
    original_open = os.open
    original_close = os.close
    ticket_fds = []

    def fail(*_args, **_kwargs):
        raise failure

    def open_ticket(path, *args, **kwargs):
        is_ticket = Path(path).suffix == ".tmp"
        if is_ticket and phase == "open":
            raise failure
        fd = original_open(path, *args, **kwargs)
        if is_ticket:
            ticket_fds.append(fd)
        return fd

    def close_ticket(fd):
        original_close(fd)
        raise failure

    with monkeypatch.context() as failing:
        failing.setattr(os, "open", open_ticket)
        if phase == "lock":
            failing.setattr(FileLock, "_acquire", fail)
        elif phase == "scan":
            failing.setattr(Path, "iterdir", fail)
        elif phase == "open":
            pass
        elif phase == "close":
            failing.setattr(os, "close", close_ticket)
        elif phase == "publish":
            failing.setattr(os, "replace", fail)
        else:
            failing.setattr(os, "write", fail)
        with pytest.raises(PermissionError) as raised:
            DeviceExecutionLock.reserve("failed admission")
        assert raised.value is failure
    queue_dir = tmp_path / "artemis-global-device.queue"
    assert list(queue_dir.glob("*.wait")) == []
    assert list(queue_dir.glob("*.tmp")) == []
    for fd in ticket_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    ticket = DeviceExecutionLock.reserve("successful successor")
    assert DeviceExecutionLock.cancel_reservation(ticket)


def test_admission_completes_short_writes(monkeypatch):
    original_write = os.write

    def short_write(fd, payload):
        return original_write(fd, payload[:7])

    monkeypatch.setattr(os, "write", short_write)
    ticket = DeviceExecutionLock.reserve("short writes remain a complete ticket")
    path = DeviceExecutionLock._reservation_paths(ticket)[0]
    owner = DeviceExecutionLock._read_owner(path)
    assert owner is not None
    assert owner.token == ticket
    assert owner.description == "short writes remain a complete ticket"


def test_admission_rejects_zero_byte_writes(monkeypatch, tmp_path):
    with monkeypatch.context() as failing:
        failing.setattr(os, "write", lambda *_args: 0)
        with pytest.raises(OSError, match="Could not write"):
            DeviceExecutionLock.reserve("zero write")
    queue_dir = tmp_path / "artemis-global-device.queue"
    assert list(queue_dir.glob("*.wait")) == []
    assert list(queue_dir.glob("*.tmp")) == []
    ticket = DeviceExecutionLock.reserve("after zero write")
    assert DeviceExecutionLock.cancel_reservation(ticket)


def test_admission_retains_the_write_failure_when_close_also_fails(monkeypatch, tmp_path):
    failure = OSError("primary write failure")
    original_open = os.open
    original_close = os.close
    ticket_fds = []

    def open_ticket(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if Path(path).suffix == ".tmp":
            ticket_fds.append(fd)
        return fd

    def fail_write(*_args):
        raise failure

    def close_ticket(fd):
        original_close(fd)
        if fd in ticket_fds:
            raise OSError("secondary close failure")

    with monkeypatch.context() as failing:
        failing.setattr(os, "open", open_ticket)
        failing.setattr(os, "write", fail_write)
        failing.setattr(os, "close", close_ticket)
        with pytest.raises(OSError) as raised:
            DeviceExecutionLock.reserve("write and close failures")
        assert raised.value is failure
        assert failure.__notes__ == [
            "Could not close the unpublished Artemis queue ticket: secondary close failure"
        ]
    queue_dir = tmp_path / "artemis-global-device.queue"
    assert list(queue_dir.glob("*.wait")) == []
    assert list(queue_dir.glob("*.tmp")) == []
    for fd in ticket_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    ticket = DeviceExecutionLock.reserve("after write and close failures")
    assert DeviceExecutionLock.cancel_reservation(ticket)


@pytest.mark.parametrize("failure", [OSError("write refused"), asyncio.CancelledError("cancelled")])
def test_failed_admission_cleanup_preserves_failure_without_publishing(
    monkeypatch, tmp_path, failure
):
    original_unlink = Path.unlink

    def fail_write(*_args):
        raise failure

    def deny_temp_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise PermissionError("temp cleanup refused")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as failing:
        failing.setattr(os, "write", fail_write)
        failing.setattr(Path, "unlink", deny_temp_cleanup)
        with pytest.raises(type(failure)) as raised:
            DeviceExecutionLock.reserve("failed unpublished admission")
        assert raised.value is failure
        assert failure.__notes__ == [
            "Could not remove the unpublished Artemis queue reservation temp file."
        ]
    queue_dir = tmp_path / "artemis-global-device.queue"
    assert list(queue_dir.glob("*.wait")) == []
    for path in queue_dir.glob("*.tmp"):
        path.unlink()
    successor = DeviceExecutionLock.reserve("after failed admission")
    assert DeviceExecutionLock.cancel_reservation(successor)


def test_admission_keeps_an_unreadable_existing_prefix_ahead(monkeypatch, tmp_path):
    queue_dir = tmp_path / "artemis-global-device.queue"
    queue_dir.mkdir()
    unknown = queue_dir / f"{5000:020d}-unknown.wait"
    unknown.write_bytes(b"incomplete owner")
    original_read = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == unknown:
            raise PermissionError("existing ticket is temporarily unreadable")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(device_lock_module, "time", SimpleNamespace(**vars(time)))
    monkeypatch.setattr(device_lock_module.time, "time_ns", lambda: 1000)
    monkeypatch.setattr(Path, "read_text", unreadable)
    ticket = DeviceExecutionLock.reserve("after unknown ticket", device_id="device-alpha")
    path = DeviceExecutionLock._reservation_paths(ticket)[0]
    assert int(path.stem.partition("-")[0]) == 5001
    contender = DeviceExecutionLock("device-alpha", queue_ticket=ticket)
    with pytest.raises(DeviceBusyError, match="earlier queued task"):
        contender.acquire(blocking=False)
    assert unknown.read_bytes() == b"incomplete owner"


def test_unreadable_reservation_during_claim_cannot_be_overtaken(monkeypatch):
    first_ticket = DeviceExecutionLock.reserve("first submitted task")
    second_ticket = DeviceExecutionLock.reserve("second submitted task")
    first = DeviceExecutionLock("emulator-5554", "first task", first_ticket)
    second = DeviceExecutionLock("emulator-5554", "second task", second_ticket)
    first_path = first._find_reserved_ticket()
    claim_written = threading.Event()
    finish_claim = threading.Event()
    acquired = threading.Event()
    errors = []
    original_write = DeviceExecutionLock._atomic_write_json
    original_read = Path.read_text
    reader_thread = threading.current_thread()

    def write_claim(cls, path, payload):
        result = original_write(path, payload)
        if path == first_path:
            claim_written.set()
            assert finish_claim.wait(timeout=2.0)
        return result

    def read_during_claim(path, *args, **kwargs):
        if (
            path == first_path
            and threading.current_thread() is reader_thread
            and not finish_claim.is_set()
        ):
            raise PermissionError("Windows ticket replacement is still visible but unreadable")
        return original_read(path, *args, **kwargs)

    def acquire_first():
        try:
            first.acquire(timeout=1.0)
            acquired.set()
        except DeviceBusyError as exc:
            errors.append(str(exc))

    monkeypatch.setattr(DeviceExecutionLock, "_atomic_write_json", classmethod(write_claim))
    monkeypatch.setattr(Path, "read_text", read_during_claim)
    thread = threading.Thread(target=acquire_first)
    thread.start()
    try:
        assert claim_written.wait(timeout=2.0)
        with pytest.raises(DeviceBusyError, match="earlier queued task"):
            second.acquire(blocking=False)
    finally:
        second.release()
        finish_claim.set()
        thread.join(timeout=2.0)
        first.release()

    assert not thread.is_alive()
    assert errors == []
    assert acquired.is_set()
    # The same formerly unreadable ticket must be gone after its owner runs.
    assert not first_path.exists()
    replacement = DeviceExecutionLock("emulator-5554", "next task")
    replacement.acquire(blocking=False)
    replacement.release()


def test_incomplete_reservation_publication_cannot_be_overtaken(monkeypatch):
    publication_started = threading.Event()
    finish_publication = threading.Event()
    original_write = os.write
    original_acquire = FileLock._acquire
    admission_waiting = threading.Event()
    tickets = []
    successor_tickets = []
    errors = []

    def write_reservation(fd, payload):
        if threading.current_thread() is thread:
            publication_started.set()
            assert finish_publication.wait(timeout=2.0)
        return original_write(fd, payload)

    def reserve_first():
        try:
            tickets.append(
                DeviceExecutionLock.reserve("first submitted task", device_id="emulator-5554")
            )
        except BaseException as exc:
            errors.append(exc)

    def reserve_second():
        try:
            successor_tickets.append(
                DeviceExecutionLock.reserve("second submitted task", device_id="emulator-5554")
            )
        except BaseException as exc:
            errors.append(exc)

    def observe_admission(lock):
        original_acquire(lock)
        if threading.current_thread() is successor and not lock.is_locked:
            admission_waiting.set()

    monkeypatch.setattr(os, "write", write_reservation)
    monkeypatch.setattr(FileLock, "_acquire", observe_admission)
    thread = threading.Thread(target=reserve_first)
    successor = threading.Thread(target=reserve_second)
    thread.start()
    try:
        assert publication_started.wait(timeout=2.0)
        successor.start()
        assert admission_waiting.wait(timeout=2.0)
        assert successor_tickets == []
        # The original earlier-task assertion applies once admission completes;
        # admission itself must wait while its predecessor is half published.
        finish_publication.set()
        thread.join(timeout=2.0)
        successor.join(timeout=2.0)
        assert errors == []
        assert len(successor_tickets) == 1
        second = DeviceExecutionLock("emulator-5554", "second task", successor_tickets[0])
        with pytest.raises(DeviceBusyError, match="earlier queued task"):
            second.acquire(blocking=False)
        second.release()
        independent = DeviceExecutionLock("device-beta", "independent device")
        independent.acquire(blocking=False)
        independent.release()
        first = DeviceExecutionLock("emulator-5554", "first task", tickets[0])
        first.acquire(blocking=False)
        first.release()
    finally:
        finish_publication.set()
        thread.join(timeout=2.0)
        if successor.ident is not None:
            successor.join(timeout=2.0)
        for ticket in tickets + successor_tickets:
            DeviceExecutionLock.cancel_reservation(ticket)

    assert not thread.is_alive()
    assert not successor.is_alive()
    assert len(tickets) == 1


@pytest.mark.parametrize(
    ("record_kind", "cleanup"),
    [
        ("ticket", "local"),
        ("ticket", "public"),
        ("owner", "local"),
        ("owner", "public"),
        ("owner", "active_listing"),
    ],
)
def test_old_unreadable_live_records_are_not_reaped(monkeypatch, record_kind, cleanup):
    lock = DeviceExecutionLock("emulator-5554", "live task")
    if record_kind == "ticket":
        ticket = DeviceExecutionLock.reserve("live queued task")
        path = next(lock.queue_dir.glob(f"*-{ticket}.wait"))
    else:
        lock.acquire()
        path = lock.path
    old_time = time.time() - DeviceExecutionLock._MALFORMED_LOCK_GRACE_SECONDS - 1
    os.utime(path, (old_time, old_time))
    original_read = Path.read_text

    def unreadable_record(read_path, *args, **kwargs):
        if read_path == path:
            raise PermissionError("temporarily unreadable live record")
        return original_read(read_path, *args, **kwargs)

    with monkeypatch.context() as read_patch:
        read_patch.setattr(Path, "read_text", unreadable_record)
        if cleanup == "public":
            assert DeviceExecutionLock.cleanup_stale_locks() == 0
        elif cleanup == "active_listing":
            assert DeviceExecutionLock.get_active_owners() == {}
        elif record_kind == "ticket":
            assert lock._remove_stale_queue_entries() == 0
        else:
            assert lock._remove_stale_lock() is False
        assert path.exists()

    assert lock._owner_is_alive(lock._read_owner(path))
    if record_kind == "ticket":
        assert DeviceExecutionLock.cancel_reservation(ticket)
    else:
        assert DeviceExecutionLock.get_active_owner("emulator-5554") is not None
        lock.release()
    assert not path.exists()


@pytest.mark.parametrize("record_kind", ["ticket", "owner"])
def test_old_malformed_records_are_still_reaped(record_kind):
    lock = DeviceExecutionLock("emulator-5554", "replacement task")
    if record_kind == "ticket":
        ticket = DeviceExecutionLock.reserve("malformed queued task")
        path = next(lock.queue_dir.glob(f"*-{ticket}.wait"))
    else:
        path = lock.path
    path.write_text("{incomplete payload", encoding="utf-8")
    old_time = time.time() - DeviceExecutionLock._MALFORMED_LOCK_GRACE_SECONDS - 1
    os.utime(path, (old_time, old_time))

    assert DeviceExecutionLock.cleanup_stale_locks() == 1
    assert not path.exists()
    lock.acquire(blocking=False)
    lock.release()


def test_reservation_removed_after_scan_does_not_block_device(monkeypatch):
    ticket = DeviceExecutionLock.reserve("cancelled during scan")
    lock = DeviceExecutionLock("emulator-5554", "next task")
    original_build = DeviceExecutionLock._build_device_queue

    def build_after_cancel(cls, wait_files, **kwargs):
        assert DeviceExecutionLock.cancel_reservation(ticket)
        return original_build(wait_files, **kwargs)

    monkeypatch.setattr(DeviceExecutionLock, "_build_device_queue", classmethod(build_after_cancel))
    lock.acquire(blocking=False)
    lock.release()


def test_younger_unreadable_ticket_does_not_block_an_older_ticket(monkeypatch):
    first_ticket = DeviceExecutionLock.reserve("first task", device_id="device-alpha")
    second_ticket = DeviceExecutionLock.reserve("second task", device_id="device-beta")
    first = DeviceExecutionLock("device-alpha", "first task", first_ticket)
    second_path = next(first.queue_dir.glob(f"*-{second_ticket}.wait"))
    original_read = Path.read_text

    def unreadable_younger(path, *args, **kwargs):
        if path == second_path:
            raise PermissionError("younger ticket is being replaced")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_younger)
    first.acquire(blocking=False)
    first.release()
    DeviceExecutionLock.cancel_reservation(second_ticket)


@pytest.mark.parametrize("max_concurrency", [1, 2])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("unreadable", ["permission", "incomplete"])
def test_global_capacity_does_not_treat_unknown_owner_as_free(
    monkeypatch, max_concurrency, legacy, unreadable
):
    owner = DeviceExecutionLock("device-alpha", "live owner")
    contender = DeviceExecutionLock("device-beta", "next owner", max_concurrency=max_concurrency)
    owner.acquire()
    path = owner._global_lock_path() if legacy else owner.path
    if legacy:
        owner.path.replace(path)
    payload = path.read_text(encoding="utf-8")
    original_read = Path.read_text

    def read_owner(path_to_read, *args, **kwargs):
        if path_to_read == path and unreadable == "permission":
            raise PermissionError("temporarily unreadable owner")
        return original_read(path_to_read, *args, **kwargs)

    try:
        if unreadable == "incomplete":
            path.write_text("{", encoding="utf-8")
        with monkeypatch.context() as read_patch:
            read_patch.setattr(Path, "read_text", read_owner)
            with pytest.raises(DeviceBusyError, match="owner record is unreadable"):
                contender.acquire(blocking=False)
            with pytest.raises(DeviceBusyError, match="Timed out waiting for a concurrency slot"):
                contender.acquire(timeout=0.0)
            assert path.exists()
            assert not contender.path.exists()
            assert list(contender.queue_dir.glob("*.wait")) == []

        path.write_text(payload, encoding="utf-8")
        if max_concurrency == 2:
            contender.acquire(blocking=False)
            contender.release()
        else:
            with pytest.raises(DeviceBusyError, match="concurrency limit"):
                contender.acquire(blocking=False)
    finally:
        path.write_text(payload, encoding="utf-8")
        if legacy:
            path.replace(owner.path)
        contender.release()
        owner.release()

    successor = DeviceExecutionLock("device-beta", "next task", max_concurrency=max_concurrency)
    successor.acquire(blocking=False)
    successor.release()


def test_waiting_for_unreadable_global_owner_can_be_cancelled(monkeypatch):
    owner = DeviceExecutionLock("device-alpha", "live owner")
    waiter = DeviceExecutionLock("device-beta", "waiting task", max_concurrency=1)
    cancel_event = threading.Event()
    original_read = Path.read_text
    owner.acquire()

    def unreadable_owner(path, *args, **kwargs):
        if path == owner.path:
            cancel_event.set()
            raise PermissionError("temporarily unreadable owner")
        return original_read(path, *args, **kwargs)

    try:
        with monkeypatch.context() as read_patch:
            read_patch.setattr(Path, "read_text", unreadable_owner)
            with pytest.raises(DeviceBusyError, match="queue was cancelled"):
                waiter.acquire(timeout=0.5, poll_interval=0.01, cancel_event=cancel_event)
        assert list(waiter.queue_dir.glob("*.wait")) == []
        assert owner.path.exists()
    finally:
        waiter.release()
        owner.release()


def test_global_capacity_preserves_session_reentrancy():
    owner = DeviceExecutionLock(
        "device-alpha", "parent task", session_id="session", max_concurrency=1
    )
    child = DeviceExecutionLock(
        "device-beta", "child task", session_id="session", max_concurrency=1
    )
    owner.acquire()
    try:
        child.acquire(blocking=False)
        assert len(DeviceExecutionLock.get_active_owners()) == 2
    finally:
        child.release()
        owner.release()


def test_pending_reservation_at_queue_head_does_not_block_other_devices():
    """An unclaimed pending submission must not gate every device's queue head.

    Regression: a reservation whose device is still "pending" used to be
    counted into every device's FIFO queue, so one unclaimed head-of-queue
    ticket blocked all devices at once. It must now park on at most one idle
    candidate device (the first in sorted order) and leave the others
    schedulable.
    """
    pending_ticket = DeviceExecutionLock.reserve("unclaimed submission")
    # device-alpha is a known idle device (a queued submission targets it), so
    # the pending ticket parks there; device-beta must stay schedulable.
    alpha_ticket = DeviceExecutionLock.reserve("task for device alpha", device_id="device-alpha")
    try:
        beta = DeviceExecutionLock("device-beta", "task for device beta")
        # Must acquire immediately even though the older pending ticket is at
        # the head of the global queue directory.
        beta.acquire(blocking=False)
        beta.release()
    finally:
        DeviceExecutionLock.cancel_reservation(pending_ticket)
        DeviceExecutionLock.cancel_reservation(alpha_ticket)


def test_pending_reservation_is_served_with_original_fifo_position_after_claim():
    """A pending reservation is not starved: once its worker claims a device it
    keeps its original submission-order position in that device's queue."""
    holder = DeviceExecutionLock("emulator-5554", "current holder")
    holder.acquire()

    # Submitted while the device is busy; the reservation stays "pending".
    first_ticket = DeviceExecutionLock.reserve("first submitted task")

    claimed = DeviceExecutionLock("emulator-5554", "claimed pending task", first_ticket)
    youngest = DeviceExecutionLock("emulator-5554", "youngest task")
    order = []
    claimed_acquired = threading.Event()
    youngest_acquired = threading.Event()

    def run_claimed():
        claimed.acquire()
        order.append("claimed")
        claimed_acquired.set()

    def run_youngest():
        youngest.acquire()
        order.append("youngest")
        youngest_acquired.set()

    t1 = threading.Thread(target=run_claimed)
    t2 = threading.Thread(target=run_youngest)
    t1.start()
    t2.start()
    time.sleep(0.2)
    assert not claimed_acquired.is_set()
    assert not youngest_acquired.is_set()

    holder.release()
    # The claimed pending ticket kept its original (oldest) FIFO timestamp and
    # must win the device before the younger concrete ticket.
    assert claimed_acquired.wait(timeout=2.0)
    assert not youngest_acquired.is_set()
    claimed.release()
    assert youngest_acquired.wait(timeout=2.0)
    youngest.release()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert order == ["claimed", "youngest"]


def test_cancelled_reservation_cannot_execute():
    ticket = DeviceExecutionLock.reserve("cancelled task")
    assert DeviceExecutionLock.cancel_reservation(ticket)

    lock = DeviceExecutionLock("emulator-5554", "cancelled task", ticket)
    with pytest.raises(DeviceBusyError, match="cancelled or expired"):
        lock.acquire()


def _reservation_with_literal_token(token):
    issued = DeviceExecutionLock.reserve("literal token task", device_id="device-queued")
    queue_dir = DeviceExecutionLock("device-queued").queue_dir
    original = next(queue_dir.glob(f"*-{issued}.wait"))
    payload = json.loads(original.read_text(encoding="utf-8"))
    payload["token"] = token
    timestamp = original.name.partition("-")[0]
    path = original.with_name(f"{timestamp}-{token}.wait")
    original.rename(path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("token", ["*", "?", "[ab]", "../*", "..\\*", "sub/*", "sub\\*"])
def test_reservation_tokens_are_literal_and_cannot_expand_scope(token):
    paths = [_reservation_with_literal_token(value) for value in ("a", "b", "foo-bar")]
    original = {path: path.read_bytes() for path in paths}
    lock = DeviceExecutionLock("device-queued", queue_ticket=token)

    assert lock._find_reserved_ticket() is None
    assert not DeviceExecutionLock.transfer_reservation(token, os.getpid())
    assert not DeviceExecutionLock.cancel_reservation(token)
    assert {path: path.read_bytes() for path in paths} == original


def test_hyphen_token_does_not_match_a_shorter_suffix():
    longer = _reservation_with_literal_token("foo-bar")
    original = longer.read_bytes()
    lock = DeviceExecutionLock("device-queued", queue_ticket="bar")

    assert lock._find_reserved_ticket() is None
    assert not DeviceExecutionLock.transfer_reservation("bar", os.getpid())
    assert not DeviceExecutionLock.cancel_reservation("bar")
    assert longer.read_bytes() == original

    exact = _reservation_with_literal_token("bar")
    assert lock._find_reserved_ticket() == exact
    assert DeviceExecutionLock.transfer_reservation("bar", os.getpid(), description="exact task")
    assert json.loads(exact.read_text(encoding="utf-8"))["description"] == "exact task"
    assert longer.read_bytes() == original
    assert DeviceExecutionLock.cancel_reservation("bar")
    assert not exact.exists()
    assert longer.read_bytes() == original


def test_bracket_token_matches_only_its_literal_filename():
    neighbors = [_reservation_with_literal_token(value) for value in ("a", "b")]
    original = {path: path.read_bytes() for path in neighbors}
    exact = _reservation_with_literal_token("[ab]")
    lock = DeviceExecutionLock("device-queued", queue_ticket="[ab]")

    assert lock._find_reserved_ticket() == exact
    assert DeviceExecutionLock.transfer_reservation("[ab]", os.getpid(), description="literal task")
    assert json.loads(exact.read_text(encoding="utf-8"))["description"] == "literal task"
    assert {path: path.read_bytes() for path in neighbors} == original
    assert DeviceExecutionLock.cancel_reservation("[ab]")
    assert not exact.exists()
    assert {path: path.read_bytes() for path in neighbors} == original


@pytest.mark.parametrize("ingress", ["argument", "environment"])
def test_invalid_ticket_admission_preserves_other_tickets_and_active_owner(monkeypatch, ingress):
    active = DeviceExecutionLock("device-active", "active task", concurrency_mode="per_device")
    active.acquire()
    active_payload = active.path.read_bytes()
    paths = [_reservation_with_literal_token(value) for value in ("a", "b")]
    original = {path: path.read_bytes() for path in paths}
    if ingress == "environment":
        monkeypatch.setenv(DeviceExecutionLock.QUEUE_TICKET_ENV, "*")
        lock = DeviceExecutionLock("device-first", concurrency_mode="per_device")
    else:
        lock = DeviceExecutionLock("device-first", queue_ticket="*", concurrency_mode="per_device")
    replacements = []

    def reject_replacement(src, dst):
        replacements.append((src, dst))
        raise PermissionError("replacement refused")

    monkeypatch.setattr(os, "replace", reject_replacement)
    try:
        with pytest.raises(DeviceBusyError, match="cancelled or expired"):
            lock.acquire(blocking=False)
        assert not replacements
        assert not lock.path.exists()
        assert {path: path.read_bytes() for path in paths} == original
        assert active.path.read_bytes() == active_payload
    finally:
        active.release()


def test_failed_reservation_rewrite_cannot_execute(monkeypatch):
    active = DeviceExecutionLock("device-beta", "active task", concurrency_mode="per_device")
    active.acquire()
    active_payload = active.path.read_bytes()
    ticket = DeviceExecutionLock.reserve("submitted task")
    unrelated = DeviceExecutionLock.reserve("unrelated task", device_id="device-gamma")
    lock = DeviceExecutionLock(
        "emulator-5554", "worker task", ticket, concurrency_mode="per_device"
    )
    path = lock._find_reserved_ticket()
    unrelated_path = next(lock.queue_dir.glob(f"*-{unrelated}.wait"))
    unrelated_payload = unrelated_path.read_bytes()
    original_replace = os.replace
    attempts = []

    def reject_claim(src, dst):
        if dst == path:
            attempts.append(dst)
            raise PermissionError("Windows replacement refused")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", reject_claim)
    successor = DeviceExecutionLock("emulator-5554", "next task", concurrency_mode="per_device")
    try:
        with pytest.raises(DeviceBusyError, match="ticket rewrite failed"):
            lock.acquire(blocking=False)

        assert len(attempts) == 3
        assert not lock._acquired
        assert not lock.path.exists()
        assert not path.exists()
        assert list(lock.queue_dir.glob("*.tmp")) == []
        assert unrelated_path.read_bytes() == unrelated_payload
        assert active.path.read_bytes() == active_payload
        # No caller cancellation or PID exit: the next owner must make progress.
        successor.acquire(timeout=0.5, poll_interval=0.01)
        assert successor._acquired
        assert unrelated_path.read_bytes() == unrelated_payload
        assert active.path.read_bytes() == active_payload
    finally:
        successor.release()
        active.release()
        DeviceExecutionLock.cancel_reservation(unrelated)


@pytest.mark.parametrize("failure", [RuntimeError("claim failed"), asyncio.CancelledError()])
def test_failed_claim_cleanup_preserves_original_exception(monkeypatch, failure):
    ticket = DeviceExecutionLock.reserve("submitted task")
    lock = DeviceExecutionLock("emulator-5554", "worker task", ticket)
    path = lock._find_reserved_ticket()

    def abort_claim(_src, _dst):
        raise failure

    monkeypatch.setattr(os, "replace", abort_claim)
    with pytest.raises(type(failure)) as raised:
        lock.acquire(blocking=False)

    assert raised.value is failure
    assert not lock.path.exists()
    assert not path.exists()
    assert list(lock.queue_dir.glob("*.tmp")) == []


def test_failed_claim_reports_cleanup_io_error_without_replacing_admission_error(monkeypatch):
    ticket = DeviceExecutionLock.reserve("submitted task")
    lock = DeviceExecutionLock("emulator-5554", "worker task", ticket)
    path = lock._find_reserved_ticket()
    original_unlink = Path.unlink

    def reject_replace(_src, _dst):
        raise PermissionError("Windows replacement refused")

    def reject_ticket_unlink(unlink_path, *args, **kwargs):
        if unlink_path == path:
            raise PermissionError("ticket cleanup refused")
        return original_unlink(unlink_path, *args, **kwargs)

    with monkeypatch.context() as io_patch:
        io_patch.setattr(os, "replace", reject_replace)
        io_patch.setattr(Path, "unlink", reject_ticket_unlink)
        with pytest.raises(DeviceBusyError, match="ticket rewrite failed") as raised:
            lock.acquire(blocking=False)

    assert path.exists()
    assert not lock.path.exists()
    assert raised.value.__notes__ == [
        "Could not cancel the failed Artemis queue reservation; its ticket remains pending."
    ]
    assert DeviceExecutionLock.cancel_reservation(ticket)


def test_reservation_can_be_transferred_to_worker_process():
    ticket = DeviceExecutionLock.reserve("submitted task")

    assert DeviceExecutionLock.transfer_reservation(ticket, 4242, description="worker task")

    path = next((DeviceExecutionLock("device").queue_dir).glob(f"*-{ticket}.wait"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == 4242
    assert payload["description"] == "worker task"


def test_waiting_acquire_can_be_cancelled_without_leaving_a_ticket():
    owner = DeviceExecutionLock("emulator-5554", "owner")
    waiter = DeviceExecutionLock("emulator-5554", "waiter")
    cancel_event = threading.Event()
    errors = []
    owner.acquire()

    def wait_for_device():
        try:
            waiter.acquire(cancel_event=cancel_event)
        except DeviceBusyError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wait_for_device)
    thread.start()
    time.sleep(0.15)
    cancel_event.set()
    thread.join(timeout=2.0)

    try:
        assert errors == ["Waiting for the Artemis device queue was cancelled."]
        assert list(waiter.queue_dir.glob("*.wait")) == []
    finally:
        owner.release()


def test_active_owner_is_discoverable_and_can_be_annotated(monkeypatch):
    monkeypatch.setenv("ARTEMIS_TASK_INGRESS", "cli")
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    owner_lock = DeviceExecutionLock("emulator-5554", "CLI task")
    owner_lock.acquire()
    try:
        owner = DeviceExecutionLock.get_active_owner()
        assert owner is not None
        assert owner.pid == owner_lock._read_owner(owner_lock.path).pid
        assert owner.ingress == "cli"
        assert owner.session_id is None
        assert DeviceExecutionLock.is_active_owner(owner)

        assert DeviceExecutionLock.annotate_active_owner(
            session_id="cli-session",
            ingress="cli",
        )
        annotated = DeviceExecutionLock.get_active_owner()
        assert annotated is not None
        assert annotated.session_id == "cli-session"
        assert annotated.ingress == "cli"
    finally:
        owner_lock.release()


def test_multi_device_locks_can_run_concurrently():
    lock_a = DeviceExecutionLock("emulator-5554", "task on device A")
    lock_b = DeviceExecutionLock("pixel-9-test", "task on device B")

    lock_a.acquire()
    try:
        # Device B should be acquired without blocking since it's a different device
        lock_b.acquire(blocking=False)
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
            assert "emulator-5554" in active_owners
            assert "pixel-9-test" in active_owners

            # A second task on device A should be blocked
            lock_a_second = DeviceExecutionLock("emulator-5554", "second task on device A")
            with pytest.raises(DeviceBusyError, match="task on device A"):
                lock_a_second.acquire(blocking=False)
        finally:
            lock_b.release()
    finally:
        lock_a.release()


def test_global_concurrency_serial_mode_blocks_other_devices(monkeypatch):
    monkeypatch.setenv("ARTEMIS_MAX_CONCURRENT_TASKS", "1")
    lock_a = DeviceExecutionLock("device_alpha", "task alpha")
    lock_b = DeviceExecutionLock("device_beta", "task beta")

    lock_a.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="concurrency limit"):
            lock_b.acquire(blocking=False)
    finally:
        lock_a.release()

    # Once lock A is released, lock B can acquire
    lock_b.acquire(blocking=False)
    lock_b.release()


def test_owner_is_alive_guards_against_recycled_pid():
    from unittest.mock import MagicMock, patch
    from artemis.runtime.device_lock import DeviceLockOwner

    owner = DeviceLockOwner(
        pid=99999,
        process_created_at=0.0,
        token="token-1",
        device_id="dev-1",
        description="test",
        acquired_at="now",
    )

    # 1. PID is 0 or negative -> immediately dead
    invalid_owner = DeviceLockOwner(
        pid=0,
        process_created_at=0.0,
        token="token-1",
        device_id="dev-1",
        description="test",
        acquired_at="now",
    )
    assert DeviceExecutionLock._owner_is_alive(invalid_owner) is False

    # 2. Recycled PID belongs to non-Artemis system process -> treated as dead
    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc.name.return_value = "systemd"
    mock_proc.cmdline.return_value = ["/sbin/init"]

    with patch("psutil.Process", return_value=mock_proc):
        assert DeviceExecutionLock._owner_is_alive(owner) is False

    # 3. PID belongs to a Python / Artemis process -> alive
    mock_proc.name.return_value = "python3"
    mock_proc.cmdline.return_value = ["python3", "-m", "artemis.main"]
    with patch("psutil.Process", return_value=mock_proc):
        assert DeviceExecutionLock._owner_is_alive(owner) is True


def test_safe_unlink_and_replace_retry(tmp_path):
    f = tmp_path / "test_file.txt"
    f.write_text("hello")

    assert DeviceExecutionLock._safe_unlink(f) is True
    assert not f.exists()

    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("world")
    assert DeviceExecutionLock._safe_replace(src, dst) is True
    assert dst.read_text() == "world"
