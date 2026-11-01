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

"""Unit and integration tests verifying MCP stdio handshake reliability,

subprocess stdin isolation against pipe hijacking, and stdout purity.
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from artemis.runtime.awake_lease import ScreenAwakeLease
from artemis.runtime.awake_service import _run_awake_adb_command
from third_party.mobile_use.clients import ui_automator_client
from third_party.mobile_use.utils.logger import get_logger
from mcp_server import server
from mcp_server.utils import device_utils, env_utils


def _readline_with_timeout(pipe, timeout: float) -> str | None:
    """Read one line from a subprocess pipe with a timeout, portably.

    select.select only supports sockets on Windows, so poll via a reader thread
    instead of selecting on the pipe handle.
    """
    result: queue.Queue = queue.Queue()

    def _reader():
        try:
            result.put(pipe.readline())
        except Exception:
            result.put(b"")

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        return None
    if not line:
        return None
    return line.decode("utf-8").strip()


@pytest.mark.parametrize("deny_artemis", [False, True], ids=["production", "deny-sdk-imports"])
def test_mcp_stdio_handshake_immediate_input(deny_artemis, tmp_path):
    """Verify that MCP server over stdio responds to an immediate initialize request

    without hanging, even when stdin data is pushed concurrently with process startup.
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    python_exe = sys.executable
    argv = [python_exe, "-m", "mcp_server"]
    if deny_artemis:
        argv = [
            python_exe,
            "-c",
            "import importlib.abc, runpy, sys\n"
            "class DenyArtemis(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname == 'artemis' or fullname.startswith('artemis.'):\n"
            "            raise AssertionError('disabled main imported SDK: ' + fullname)\n"
            "sys.meta_path.insert(0, DenyArtemis())\n"
            "runpy.run_module('mcp_server', run_name='__main__')\n",
        ]

    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PYTHONUNBUFFERED": "1",
            "ARTEMIS_KEEP_DEVICE_AWAKE": "false",
            "PYTHON_DOTENV_DISABLED": "1",
            "ARTEMIS_APP_DIR": str(tmp_path / "artemis"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": project_root,
            # Windows: the interpreter's socket stack (Winsock/_overlapped),
            # Path.home(), and tempfile handling need these system variables.
            **{
                key: os.environ[key]
                for key in (
                    "SYSTEMROOT",
                    "SYSTEMDRIVE",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "HOMEDRIVE",
                    "HOMEPATH",
                    "APPDATA",
                    "LOCALAPPDATA",
                )
                if key in os.environ
            },
            "APPDATA": str(tmp_path / "appdata"),
            "LOCALAPPDATA": str(tmp_path / "localapp"),
        },
        cwd=project_root,
    )
    stderr = []

    def drain_stderr():
        stderr.extend(p.stderr)

    stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
    stderr_reader.start()

    try:
        # Immediately write the JSON-RPC initialize request into stdin
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
        p.stdin.write(json.dumps(init_req).encode("utf-8") + b"\n")
        p.stdin.flush()

        # Read initialize response with a strict timeout
        init_resp_line = _readline_with_timeout(p.stdout, 6.0)

        assert init_resp_line is not None, (
            "MCP server failed to respond to initialize request within 6 seconds (deadlock detected)!"
        )
        init_data = json.loads(init_resp_line)
        assert init_data.get("id") == 1
        assert "result" in init_data

        # Follow up with initialized notification and tools/list request
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        p.stdin.write(json.dumps(notif).encode("utf-8") + b"\n")
        p.stdin.write(json.dumps(tools_req).encode("utf-8") + b"\n")
        p.stdin.flush()

        tools_resp_line = _readline_with_timeout(p.stdout, 4.0)

        assert tools_resp_line is not None, "MCP server failed to respond to tools/list request!"
        tools_data = json.loads(tools_resp_line)
        assert tools_data.get("id") == 2
        tools = tools_data.get("result", {}).get("tools", [])
        tool_names = {t["name"] for t in tools}
        assert {
            "mobile_run_task",
            "mobile_manage_task",
            "mobile_get_device_state",
            "mobile_inspect_trace",
            "mobile_diagnose",
        }.issubset(tool_names)

    finally:
        p.stdin.close()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)
        stderr_reader.join(timeout=3)
    assert p.returncode == 0, b"".join(stderr).decode("utf-8", errors="replace")
    assert b"disabled main imported SDK" not in b"".join(stderr)


@pytest.mark.parametrize("transport", ["stdio", "sse"])
@pytest.mark.parametrize("run_fails", [False, True])
@pytest.mark.parametrize("awake_setting", [None, "true", "1", "yes", "on", ""])
def test_main_preserves_awake_service_lifecycle(transport, run_fails, awake_setting, monkeypatch):
    """Awake still starts in a daemon thread and shuts down even if serving fails."""
    events = []
    if awake_setting is None:
        monkeypatch.delenv("ARTEMIS_KEEP_DEVICE_AWAKE", raising=False)
    else:
        monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", awake_setting)

    def run(**kwargs):
        events.append("serve")
        if run_fails:
            raise RuntimeError("test serving failure")

    with (
        patch("artemis.runtime.start_awake_service", side_effect=lambda: events.append("awake")),
        patch("artemis.runtime.shutdown_awake_service", side_effect=lambda: events.append("stop")),
        patch.object(server.threading, "Thread") as thread,
        patch.object(server.mcp, "run", side_effect=run) as serve,
    ):
        thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
        if run_fails:
            with pytest.raises(RuntimeError, match="test serving failure"):
                server.main(transport=transport, host="127.0.0.2", port=8123)
        else:
            server.main(transport=transport, host="127.0.0.2", port=8123)

        assert thread.call_args.kwargs["daemon"] is True
        assert thread.call_args.kwargs["name"] == "artemis-awake-init"
        thread.return_value.start.assert_called_once_with()
        expected = {"transport": transport}
        if transport == "sse":
            expected.update(host="127.0.0.2", port=8123)
        serve.assert_called_once_with(**expected)
    assert events == ["awake", "serve", "stop"]


@pytest.mark.parametrize(
    "awake_setting", ["0", "false", "no", "off", " 0 ", " FALSE ", "\tNo\n", " OFF "]
)
def test_disabled_main_does_not_bootstrap_unused_runtime(awake_setting, monkeypatch):
    monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", awake_setting)
    monkeypatch.delitem(sys.modules, "artemis.runtime", raising=False)
    with (
        patch.object(server.threading, "Thread") as thread,
        patch.object(server.env_utils, "import_artemis_package") as bootstrap,
        patch.object(server.mcp, "run") as serve,
    ):
        server.main()
    thread.assert_not_called()
    bootstrap.assert_not_called()
    serve.assert_called_once_with(transport="stdio")


@pytest.mark.parametrize("run_fails", [False, True])
def test_disabled_main_cleans_up_runtime_loaded_by_tools(run_fails, monkeypatch):
    monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", "false")
    events = []

    def run(**kwargs):
        events.append("serve")
        if run_fails:
            raise RuntimeError("test serving failure")

    with (
        patch("artemis.runtime.shutdown_awake_service", side_effect=lambda: events.append("stop")),
        patch.object(server.threading, "Thread") as thread,
        patch.object(server.mcp, "run", side_effect=run),
    ):
        if run_fails:
            with pytest.raises(RuntimeError, match="test serving failure"):
                server.main()
        else:
            server.main()
    thread.assert_not_called()
    assert events == ["serve", "stop"]


def test_enabled_main_preserves_shutdown_before_delayed_awake_start(monkeypatch):
    monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", "true")
    entered, release = threading.Event(), threading.Event()
    awake_threads = []
    events = []

    def bootstrap():
        if threading.current_thread().name == "artemis-awake-init":
            awake_threads.append(threading.current_thread())
            entered.set()
            assert release.wait(timeout=3)

    def run(**kwargs):
        assert entered.wait(timeout=3)
        events.append("serve")

    with (
        patch.object(server.env_utils, "import_artemis_package", side_effect=bootstrap),
        patch("artemis.runtime.start_awake_service", side_effect=lambda: events.append("awake")),
        patch("artemis.runtime.shutdown_awake_service", side_effect=lambda: events.append("stop")),
        patch.object(server.mcp, "run", side_effect=run),
    ):
        try:
            server.main()
            assert events == ["serve", "stop"]
        finally:
            release.set()
            for thread in awake_threads:
                thread.join(timeout=3)
                assert not thread.is_alive()
    assert events == ["serve", "stop", "awake"]


def test_awake_service_adb_command_isolates_stdin():
    """Verify _run_awake_adb_command always sets stdin=subprocess.DEVNULL."""
    with patch("artemis.runtime.awake_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _run_awake_adb_command("test-dev-1", ["shell", "date"], "test command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_awake_lease_run_isolates_stdin():
    """Verify ScreenAwakeLease._run always sets stdin=subprocess.DEVNULL."""
    lease = ScreenAwakeLease("test-dev-1")
    with patch("artemis.runtime.awake_lease.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        lease._run(["shell", "date"], "test lease command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_detached_process_kwargs_isolates_stdin():
    """Verify get_detached_process_kwargs always sets stdin=subprocess.DEVNULL."""
    kwargs = env_utils.get_detached_process_kwargs()
    assert kwargs.get("stdin") == subprocess.DEVNULL, (
        "Expected stdin=subprocess.DEVNULL for detached background tasks!"
    )


def test_device_utils_isolates_stdin():
    """Verify device_utils subprocess calls always set stdin=subprocess.DEVNULL."""
    with patch("mcp_server.utils.device_utils.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="List of devices attached\n", stderr=""
        )
        device_utils.get_connected_devices()
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_ui_automator_client_isolates_stdin():
    """Verify ui_automator_client helper commands isolate stdin."""
    with patch("third_party.mobile_use.clients.ui_automator_client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="package:com.test\n", stderr="")
        ui_automator_client._is_package_installed("dev-1", "com.test")
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL

    with patch("third_party.mobile_use.clients.ui_automator_client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ui_automator_client._uninstall_package("dev-1", "com.test")
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_logger_header_does_not_pollute_stdout():
    """Verify logger.header directs output to stderr and never pollutes sys.stdout."""
    logger = get_logger("test_purity_logger")
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    try:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr
        logger.header("IMPORTANT PROTOCOL BANNER")
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr

    assert captured_stdout.getvalue() == "", (
        f"Detected stdout pollution from logger.header: {captured_stdout.getvalue()!r}. "
        "In MCP stdio mode, stdout MUST be reserved exclusively for JSON-RPC messages!"
    )
    assert "IMPORTANT PROTOCOL BANNER" in captured_stderr.getvalue()
