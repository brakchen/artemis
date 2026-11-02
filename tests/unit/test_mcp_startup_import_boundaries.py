"""MCP metadata import boundary and public Python API compatibility."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _run_isolated(code: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
        )
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(root),
        LOCALAPPDATA=str(tmp_path / "localapp"),
        APPDATA=str(tmp_path / "appdata"),
        ARTEMIS_APP_DIR=str(tmp_path / "artemis"),
        ARTEMIS_KEEP_DEVICE_AWAKE="false",
        PYTHON_DOTENV_DISABLED="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_imports_keep_identity_star_exports_and_legacy_aliases(tmp_path):
    _run_isolated(
        """
        import importlib
        import warnings
        import artemis
        from artemis import (
            Agent, ArtemisClient, BaseDeviceDriver, Builders, Capabilities,
            ConcurrencyMode, Device, StreamEvent, StreamEventType, Task,
            TaskHandle, TaskResult,
        )
        from artemis._version import __version__
        from artemis.sdk.agent import Agent as CanonicalAgent
        from artemis.sdk.builders import Builders as CanonicalBuilders
        from artemis.drivers.base import BaseDeviceDriver as CanonicalDriver
        from artemis.interfaces.sdk.client import ConcurrencyMode as CanonicalMode
        from artemis.interfaces.sdk.task import (
            StreamEvent as CanonicalEvent, StreamEventType as CanonicalEventType,
            Task as CanonicalTask,
        )
        import artemis_client

        expected = [
            "__version__", "ArtemisClient", "Capabilities", "ConcurrencyMode",
            "Device", "StreamEvent", "StreamEventType", "Agent", "Builders",
            "Task", "TaskHandle", "TaskResult", "BaseDeviceDriver",
        ]
        assert artemis.__all__ == expected
        assert artemis.__version__ == __version__
        assert Agent is CanonicalAgent
        assert Builders is CanonicalBuilders
        assert BaseDeviceDriver is CanonicalDriver
        assert ConcurrencyMode is CanonicalMode
        assert StreamEvent is CanonicalEvent
        assert StreamEventType is CanonicalEventType
        assert Task is CanonicalTask
        for name in ("ArtemisClient", "Capabilities", "Device", "TaskHandle", "TaskResult"):
            assert getattr(artemis, name) is getattr(artemis_client, name)
        namespace = {}
        exec("from artemis import *", namespace)
        assert set(namespace) - {"__builtins__"} == set(expected)
        for name in expected:
            assert namespace[name] is vars(artemis)[name]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for old, new in artemis._legacy_imports.LEGACY_MODULES.items():
                assert importlib.import_module(old) is importlib.import_module(new)
        """,
        tmp_path,
    )


def test_mcp_tool_metadata_does_not_load_artemis_execution_modules(tmp_path):
    _run_isolated(
        """
        import asyncio
        import importlib.abc
        import sys

        class DenyExecution(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "artemis" or fullname.startswith("artemis."):
                    raise AssertionError("metadata loaded execution module: " + fullname)

        sys.meta_path.insert(0, DenyExecution())
        from mcp_server.base import mcp

        tools = asyncio.run(mcp.list_tools())
        assert {tool.name for tool in tools} == {
            "mobile_run_task", "mobile_manage_task", "mobile_get_device_state",
            "mobile_inspect_trace", "mobile_diagnose",
        }
        assert all(tool.inputSchema["type"] == "object" for tool in tools)
        """,
        tmp_path,
    )


def test_disabled_main_loads_runtime_only_for_first_tool_and_still_cleans_up(tmp_path):
    _run_isolated(
        """
        import asyncio
        import builtins
        import importlib
        import importlib.abc
        import platform
        import sys
        from unittest.mock import patch

        platform.system()
        loop = asyncio.new_event_loop()

        def deny_external(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen"}:
                raise AssertionError("external operation prohibited: " + event)

        sys.addaudithook(deny_external)
        from mcp_server import server

        class DenyArtemis(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "artemis" or fullname.startswith("artemis."):
                    raise AssertionError("disabled metadata imported SDK: " + fullname)

        deny = DenyArtemis()
        sys.meta_path.insert(0, deny)
        events = []
        real_import = builtins.__import__
        real_import_module = importlib.import_module

        def fake_controller(*args, **kwargs):
            events.append("controller")
            raise RuntimeError("canonical-disabled-first-tool")

        def import_module(name, package=None):
            if name == "artemis":
                events.append("root")
            module = real_import_module(name, package)
            if name == "artemis":
                sys.modules["artemis.runtime"].shutdown_awake_service = lambda: events.append("stop")
            return module

        def import_dependency(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "artemis.mcp.adb_server" and "_get_controller" in fromlist:
                events.append("leaf")
                module = real_import(name, globals, locals, fromlist, level)
                module._get_controller = fake_controller
                return module
            return real_import(name, globals, locals, fromlist, level)

        def run(**kwargs):
            assert "artemis" not in sys.modules
            tools = loop.run_until_complete(server.mcp.list_tools())
            assert len(tools) == 5
            assert "artemis" not in sys.modules
            sys.meta_path.remove(deny)
            response = loop.run_until_complete(server.mcp.call_tool(
                "mobile_get_device_state", {"view_type": "hierarchy", "device_serial": "fake-disabled-device"}
            ))
            assert "canonical-disabled-first-tool" in str(response), response

        with (
            patch.object(server, "threading") as threads,
            patch.object(server.mcp, "run", side_effect=run),
            patch.object(importlib, "import_module", import_module),
            patch.object(builtins, "__import__", import_dependency),
        ):
            server.main()
            threads.Thread.assert_not_called()
        loop.close()
        assert events == ["root", "leaf", "controller", "root", "stop"], events
        """,
        tmp_path,
    )


@pytest.mark.parametrize(
    "case",
    [
        "public_pair",
        "all_public",
        "public_agent_direct_builders",
        "public_builders_direct_agent",
        "builders_public_direct_config",
        "builders_config_direct_public",
        "all_public_and_direct",
        "all_direct_and_public",
    ],
)
def test_concurrent_cold_public_and_direct_imports_keep_identity(case, tmp_path):
    code = (
        "case = "
        + repr(case)
        + "\n"
        + textwrap.dedent(
            """
        import importlib
        import queue
        import threading
        import time
        import traceback
        import artemis

        exports = {
            "ArtemisClient": ("artemis.interfaces.sdk.client", "ArtemisClient"),
            "Capabilities": ("artemis_client", "Capabilities"),
            "ConcurrencyMode": ("artemis.interfaces.sdk.client", "ConcurrencyMode"),
            "Device": ("artemis_client", "Device"),
            "StreamEvent": ("artemis.interfaces.sdk.task", "StreamEvent"),
            "StreamEventType": ("artemis.interfaces.sdk.task", "StreamEventType"),
            "Agent": ("artemis.sdk.agent", "Agent"),
            "Builders": ("artemis.sdk.builders", "Builders"),
            "Task": ("artemis.interfaces.sdk.task", "Task"),
            "TaskHandle": ("artemis_client", "TaskHandle"),
            "TaskResult": ("artemis_client", "TaskResult"),
            "BaseDeviceDriver": ("artemis.drivers.base", "BaseDeviceDriver"),
        }
        operations = [("artemis", "Agent"), ("artemis", "Builders")]
        if case == "all_public":
            operations = [("artemis", name) for name in exports] * 2
        elif case in {"all_public_and_direct", "all_direct_and_public"}:
            operations = [("artemis", name) for name in exports] + list(exports.values())
            if case == "all_direct_and_public":
                operations.reverse()
        elif case == "public_agent_direct_builders":
            operations[1] = ("artemis.sdk.builders", "Builders")
        elif case == "public_builders_direct_agent":
            operations[0] = ("artemis.sdk.agent", "Agent")
        elif case.startswith("builders_"):
            operations = [
                ("artemis.sdk.builders", "Builders"),
                ("artemis.sdk.builders.agent_config_builder", "AgentConfigBuilder"),
            ]
            if case == "builders_config_direct_public":
                operations.reverse()

        barrier = threading.Barrier(len(operations))
        results = queue.Queue()

        def resolve(module, name):
            try:
                barrier.wait(timeout=10)
                value = getattr(importlib.import_module(module), name)
                results.put((module, name, value, None))
            except BaseException:
                results.put((module, name, None, traceback.format_exc()))

        threads = [
            threading.Thread(target=resolve, args=operation, daemon=True)
            for operation in operations
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 30
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        assert not any(thread.is_alive() for thread in threads), "concurrent imports hung"
        records = [results.get_nowait() for _ in threads]
        errors = [error for _, _, _, error in records if error]
        assert not errors, "\\n".join(errors)
        for module, name, value, _ in records:
            canonical_module, attribute = exports.get(
                name, ("artemis.sdk.builders.agent_config_builder", name)
            )
            assert value is getattr(importlib.import_module(canonical_module), attribute)
            assert value is getattr(importlib.import_module(module), name)
        """
        )
    )
    _run_isolated(code, tmp_path)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("kind", ["device", "diagnose", "history", "task"])
def test_main_first_tool_bootstraps_root_before_leaf(kind, warm, tmp_path):
    code = f"kind = {kind!r}\nwarm = {warm!r}\n" + textwrap.dedent(
        """
        import asyncio
        import builtins
        import importlib
        import os
        import platform
        import sys
        import threading
        import traceback
        from unittest.mock import patch

        # aiohttp queries this cached stdlib value during the product import.
        # Windows may need its OS-only subprocess fallback to populate it.
        platform.system()
        # Windows uses a local socketpair for the event-loop self-pipe.
        loop = asyncio.new_event_loop()

        def deny_external(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen"}:
                raise AssertionError("external operation prohibited: " + event)

        sys.addaudithook(deny_external)
        from mcp_server import server
        from mcp_server.tools import diagnose

        assert "artemis" not in sys.modules
        if warm:
            importlib.import_module("artemis")
        targets = {
            "device": "artemis.mcp.adb_server",
            "diagnose": "artemis.core.diagnostics.readiness",
            "history": "artemis.data_engine.history_reader",
            "task": "artemis.config.runtime",
        }
        target = targets[kind]
        barrier = threading.Barrier(2)
        crossed = set()
        awake_threads = []
        errors = []
        fake_calls = []
        responses = []
        real_import = builtins.__import__
        real_import_module = importlib.import_module
        real_exists = os.path.exists
        marker = "canonical-fake-" + kind

        def coordinate(name):
            thread = threading.current_thread()
            role = (
                "awake" if thread.name == "artemis-awake-init" and name in {
                    "artemis", "artemis.runtime"
                } else "handler" if thread.name == "MainThread" and name in {
                    "artemis", target
                } else None
            )
            if role is not None and role not in crossed:
                crossed.add(role)
                if role == "awake":
                    awake_threads.append(thread)
                barrier.wait(timeout=10)

        def fake_dependency(*args, **kwargs):
            fake_calls.append((args, kwargs))
            if kind == "task":
                raise ValueError(marker)
            raise RuntimeError(marker)

        async def fake_readiness():
            fake_calls.append("readiness")
            raise TimeoutError(marker)

        def coordinated_import(name, globals=None, locals=None, fromlist=(), level=0):
            coordinate(name)
            module = real_import(name, globals, locals, fromlist, level)
            if name == "artemis.runtime" and fromlist and "start_awake_service" in fromlist:
                module.start_awake_service = lambda: None
                module.shutdown_awake_service = lambda: None
            if kind == "device" and name == target and fromlist and "_get_controller" in fromlist:
                module._get_controller = fake_dependency
            elif kind == "diagnose" and name == target and fromlist and "collect_readiness" in fromlist:
                module.collect_readiness = fake_readiness
            elif kind == "history" and name == target and fromlist and "OfflineHistoryReader" in fromlist:
                module.OfflineHistoryReader = fake_dependency
            elif kind == "task" and name == "artemis.config" and fromlist and "checker_overrides_for_level" in fromlist:
                module.checker_overrides_for_level = fake_dependency
            return module

        def coordinated_import_module(name, package=None):
            coordinate(name)
            return real_import_module(name, package)

        def run(**kwargs):
            tools = {
                "device": ("mobile_get_device_state", {
                    "view_type": "hierarchy", "device_serial": "fake-first-device",
                }),
                "diagnose": ("mobile_diagnose", {}),
                "history": ("mobile_inspect_trace", {
                    "action": "search", "trace_id": "fake-first-history", "query": "fake",
                }),
                "task": ("mobile_run_task", {
                    "task_desc": "offline validation only", "model": "Pro",
                    "verification_level": "fake-level",
                }),
            }
            name, arguments = tools[kind]
            try:
                responses.append(str(loop.run_until_complete(server.mcp.call_tool(name, arguments))))
            except Exception as exc:
                if kind != "task" or marker not in str(exc):
                    raise
                responses.append(str(exc))

        def thread_error(args):
            errors.append("".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback
            )))

        with (
            patch.object(builtins, "__import__", coordinated_import),
            patch.object(importlib, "import_module", coordinated_import_module),
            patch.object(server.mcp, "run", side_effect=run),
            patch.object(threading, "excepthook", thread_error),
            patch.object(diagnose, "_timeout_response", return_value={"verdict": marker}),
            patch.object(os.path, "exists", side_effect=lambda path: (
                kind == "history" and str(path).endswith("data_engine.db")
            ) or real_exists(path)),
        ):
            try:
                # This case deliberately exercises enabled concurrent startup;
                # the canonical runtime producers above cannot touch a device.
                os.environ["ARTEMIS_KEEP_DEVICE_AWAKE"] = "true"
                server.main()
            finally:
                for thread in awake_threads:
                    thread.join(timeout=15)
                    assert not thread.is_alive(), "awake bootstrap did not finish"
        loop.close()
        assert crossed == {"awake", "handler"}, crossed
        assert not errors, "\\n".join(errors)
        assert len(fake_calls) == 1, fake_calls
        assert len(responses) == 1 and marker in responses[0], responses
        if kind == "device":
            assert fake_calls[0][1] == {"device_serial": "fake-first-device"}
        """
    )
    _run_isolated(code, tmp_path)
