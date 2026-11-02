"""D3 actual admission, private diagnostics and container boundary contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from pydantic import SecretStr
import pytest

_HTTPX_SEND = httpx.AsyncClient.send


@pytest.fixture
def selected_config(tmp_path, monkeypatch, isolated_llm_provider_registry):
    from artemis.llm import providers
    from artemis.sdk import Agent
    from artemis.llm.router import ModelFactory

    monkeypatch.setattr(
        Agent, "init", AsyncMock(side_effect=AssertionError("SDK initialization forbidden"))
    )
    monkeypatch.setattr(
        ModelFactory,
        "create_model",
        MagicMock(side_effect=AssertionError("model client forbidden")),
    )
    registry = providers.LLMProviderRegistry(tmp_path / "private-registry.json")
    for name, key, base in (
        ("mm-main", "SYNTHETIC-MAIN-KEY", "https://one.invalid/anthropic"),
        ("mm-backup", "SYNTHETIC-BACKUP-KEY", "https://two.invalid/anthropic"),
        ("unused", "SYNTHETIC-UNUSED-KEY", "https://unused.invalid/anthropic"),
    ):
        registry.add(
            name=name,
            api_key=key,
            api_base=base,
            kind="anthropic",
            vendor="minimax",
            model="MiniMax-M3",
            fallback_model="MiniMax-M3",
        )
    registry.add(
        name="disabled-entry",
        api_key="SYNTHETIC-DISABLED-KEY",
        api_base="https://disabled.invalid/anthropic",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3",
        enabled=False,
    )
    monkeypatch.setattr(providers, "provider_registry", registry)
    main = tmp_path / "artemis.jsonc"
    data = {
        "default": {
            "provider": "mm-main",
            "model": "MiniMax-M3",
            "fallback": {"provider": "mm-backup", "model": "MiniMax-M3"},
        },
        "agent": {"ocr": {"automatic": False}},
        "presets": {"unused": {"provider": "unused", "model": "MiniMax-M3"}},
    }
    main.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(main))
    monkeypatch.delenv("ARTEMIS_LLM_CONFIG_JSON", raising=False)
    monkeypatch.delenv("ARTEMIS_CONFIG_READ_ONLY", raising=False)
    # Every request in these tests must be explicit synthetic MockTransport.
    monkeypatch.setattr(
        httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("live HTTP forbidden"))
    )
    return main, data, registry


def invalidate(main, data, kind):
    if kind == "mixed":
        data["nodes"] = {"operator": {"provider": "ollama", "model": "local"}}
    elif kind == "aux":
        data["agent"]["flash"] = {"step_summarizer": {"model": "gemini-2.5-flash"}}
    elif kind == "disabled":
        data["default"]["provider"] = "disabled-entry"
    else:
        data["default"]["provider"] = "unknown-provider"
    main.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.asyncio
async def test_public_probes_and_nested_cached_report_have_no_secrets(selected_config, monkeypatch):
    from artemis.config import settings
    from artemis.core.diagnostics.engine import ReadinessEngine
    from artemis.core.diagnostics.probes.credentials_probe import (
        LLMCredentialsProbe,
        VisionOCRProbe,
    )

    monkeypatch.setattr(
        type(settings),
        "get_api_key",
        lambda self, provider: SecretStr("SYNTHETIC-" + provider + "-HOST-KEY"),
    )
    engine = ReadinessEngine()
    engine._probes = {"gemini_api_key": LLMCredentialsProbe(), "vision_ocr_key": VisionOCRProbe()}
    first = await engine.run_all()
    cached = await engine.run_all()
    assert first.model_dump() == cached.model_dump()
    payload = json.dumps(
        {"report": first.model_dump(), "choices": [cached.model_dump()]}, default=str
    )
    assert "SYNTHETIC" not in payload
    for name in ("raw_key", "current_key", "current_gemini_key", "api_keys"):
        assert name not in payload
    llm, ocr = first.probes
    assert llm.metadata["configured"] is True and llm.metadata["verified"] is False
    assert ocr.metadata == {
        "configured": True,
        "verified": False,
        "disabled": True,
        "source": "default_endpoint",
        "automatic": False,
    }


@pytest.mark.asyncio
async def test_internal_catalog_verification_uses_distinct_actual_fallback_auth(
    selected_config, monkeypatch
):
    from artemis.core.diagnostics.probes.credentials_probe import LLMCredentialsProbe
    from mcp_server.tools.diagnose import _verify_credentials
    from artemis.utils import credentials_validator

    calls = []

    async def verify(provider, key, **kwargs):
        calls.append((provider, key, kwargs))
        return True, "authenticated " + key + " SYNTHETIC-BACKUP-KEY"

    monkeypatch.setattr(credentials_validator, "validate_api_key", verify)
    probe = await LLMCredentialsProbe().probe()
    # Metadata can neither inject an unrelated key nor override a canonical target.
    probe.metadata["providers"] = [{"provider": "google", "raw_key": "SYNTHETIC-HOST-KEY"}]
    results = await _verify_credentials(probe)
    assert len(calls) == 2
    assert {call[1] for call in calls} == {"SYNTHETIC-MAIN-KEY", "SYNTHETIC-BACKUP-KEY"}
    assert {call[2]["base_url"] for call in calls} == {
        "https://one.invalid/anthropic",
        "https://two.invalid/anthropic",
    }
    assert all(call[0] == "anthropic" and call[2]["vendor"] == "minimax" for call in calls)
    assert all(result["valid"] for result in results)
    assert "SYNTHETIC" not in repr(results)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mixed", "aux", "unknown", "disabled"])
@pytest.mark.parametrize("profile", ["flash", "pro", "named-missing"])
async def test_api_and_direct_queue_reject_before_any_device_or_queue_work(
    selected_config, monkeypatch, kind, profile
):
    from apps.admin_console.routers import tasks
    from apps.admin_console.services.task_queue_service import TaskQueueService
    from apps.admin_console.schemas.task_schema import RunRequest
    from artemis.config.task import prepare_task_submission
    from fastapi import HTTPException

    main, data, _ = selected_config
    invalidate(main, data, kind)
    monkeypatch.setattr(tasks.session_repo, "get_session_by_id", lambda _: None)
    monkeypatch.setattr(tasks.state, "queue_items", [])
    monkeypatch.setattr(tasks.state, "active_session_id", None)
    monkeypatch.setattr(tasks.state, "active_connections", {})
    device = AsyncMock(side_effect=AssertionError("device touched before preflight"))
    monkeypatch.setattr(tasks.device_pool, "validate_explicit_serial_async", device)
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", device)
    worker = MagicMock(side_effect=AssertionError("worker started before preflight"))
    reserve = MagicMock(side_effect=AssertionError("queue mutated before preflight"))
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", worker)
    monkeypatch.setattr(TaskQueueService, "_create_queue_item", reserve)
    monkeypatch.setattr(TaskQueueService, "_find_duplicate_submission", lambda *args: None)
    with pytest.raises(ValueError):
        prepare_task_submission(profile)
    with pytest.raises(HTTPException) as error:
        await tasks.run_task(
            RunRequest(goal="synthetic", profile=profile, device_serial="synthetic")
        )
    assert error.value.status_code == 422
    with pytest.raises(ValueError):
        await TaskQueueService.enqueue_tasks(
            ["synthetic"], profile=profile, device_serial="synthetic"
        )
    device.assert_not_called()
    worker.assert_not_called()
    reserve.assert_not_called()
    assert tasks.state.queue_items == []


@pytest.mark.asyncio
async def test_accepted_api_and_queue_retry_skips_invalid_config_and_worker(
    selected_config, monkeypatch
):
    from apps.admin_console.routers import tasks
    from apps.admin_console.schemas.task_schema import RunRequest
    from apps.admin_console.services.task_queue_service import TaskQueueService
    import artemis.config.task as config_task

    fail = MagicMock(side_effect=AssertionError("accepted retry re-preflighted"))
    monkeypatch.setattr(config_task, "prepare_task_submission", fail)
    monkeypatch.setattr(
        tasks.state, "queue_items", [{"session_id": "accepted", "status": "pending"}]
    )
    monkeypatch.setattr(tasks.session_repo, "get_session_by_id", lambda _: None)
    response = await tasks.run_task(RunRequest(goal="synthetic", session_id="accepted"))
    assert response["tasks"][0]["session_id"] == "accepted"
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", fail)
    response = await TaskQueueService.enqueue_tasks(["synthetic"], session_id="accepted")
    assert response["tasks"][0]["session_id"] == "accepted"
    fail.assert_not_called()


@pytest.mark.parametrize("model", ["Flash", "Pro"])
def test_mcp_parent_rejects_before_trace_device_daemon_queue(selected_config, monkeypatch, model):
    from mcp_server.tools.task_runner import mobile_run_task
    from artemis import runtime
    from mcp_server.tools import task_runner

    main, data, _ = selected_config
    invalidate(main, data, "aux")
    fail = MagicMock(side_effect=AssertionError("admission side effect"))
    monkeypatch.setattr(runtime.trace_store, "init_trace", fail)
    monkeypatch.setattr(runtime, "ensure_daemon_running", fail)
    monkeypatch.setattr(runtime.DeviceExecutionLock, "reserve", fail)
    monkeypatch.setattr(task_runner, "_validate_device_serial", fail)
    with pytest.raises(ValueError):
        mobile_run_task("synthetic", model=model, device_serial="synthetic")
    fail.assert_not_called()


@pytest.mark.asyncio
async def test_worker_rejects_before_adb_and_emulator(selected_config, monkeypatch, tmp_path):
    from mcp_server.background import task_runner
    from artemis.runtime import trace_store

    main, data, _ = selected_config
    invalidate(main, data, "aux")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    trace_store.init_trace("synthetic", "goal", "Flash", "")
    fail = MagicMock(side_effect=AssertionError("worker touched device"))
    for name in ("resolve_adb_path", "get_connected_devices", "ensure_emulator"):
        monkeypatch.setattr(task_runner.device_utils, name, fail)
    monkeypatch.setattr(task_runner, "notify", MagicMock())
    # The worker owns its process logging; do not replace pytest/global handlers.
    monkeypatch.setattr(task_runner.logging, "basicConfig", lambda **kwargs: None)
    await task_runner.run_task("synthetic", "goal", "Flash", "")
    assert trace_store.read_status("synthetic")["status"] == "failed"
    fail.assert_not_called()


def test_mcp_shared_legacy_override_and_explicit_main_precedence(
    selected_config, monkeypatch, tmp_path
):
    from mcp_server.utils import task_config

    main, _, _ = selected_config
    override = tmp_path / "legacy"
    override.mkdir()
    (override / "llm-config.override.jsonc").write_text(
        json.dumps({"default": {"model": "MiniMax-M3.1-Flash"}})
    )
    monkeypatch.setenv("ARTEMIS_CONFIG_DIR", str(override))
    assert task_config.resolve_profile_file() is None
    assert task_config.prepare_task_config("Flash").llm_config.default.model == "MiniMax-M3"
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
    # Keep real canonical main loading synthetic while allowing legacy discovery.
    monkeypatch.setattr("artemis.config.llm.get_main_config_path", lambda _: main)
    monkeypatch.setattr("artemis.config.agent.get_main_config_path", lambda _: main)
    assert task_config.resolve_profile_file() == str(override / "llm-config.override.jsonc")
    assert task_config.prepare_task_config("Pro").llm_config.default.model == "MiniMax-M3.1-Flash"


@pytest.fixture
def docker_service(tmp_path, monkeypatch):
    config = SimpleNamespace(
        DOCKER_SOCKET_PATH="fake",
        DOCKER_NETWORK="artemis-net",
        ARTEMIS_CONTAINER_PREFIX="artemis-session",
        ARTEMIS_IMAGE="session:test",
        ARTEMIS_EXPORT_ROOT=tmp_path / "exports",
        ARTEMIS_EXPORT_HOST_ROOT="/docker-host/configs",
    )
    fake_app_config = ModuleType("app.config")
    fake_app_config.settings = config
    monkeypatch.setitem(sys.modules, "app.config", fake_app_config)
    fake_docker = ModuleType("docker")
    fake_docker.DockerClient = MagicMock()
    fake_errors = ModuleType("docker.errors")
    fake_errors.DockerException = RuntimeError
    fake_errors.NotFound = type("NotFound", (Exception,), {})
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)
    path = (
        Path(__file__).resolve().parents[2]
        / "playground/backend_manager/app/services/docker_service.py"
    )
    spec = importlib.util.spec_from_file_location("d3_docker_service", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    service = module.DockerManagerService()
    return service, config


@pytest.mark.asyncio
async def test_container_exact_mounts_keys_and_same_loader(
    selected_config, docker_service, monkeypatch
):
    from artemis.config.task import prepare_task_submission
    from artemis.llm import providers
    from artemis.llm.providers import _verify_registry_file_permissions

    service, config = docker_service
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC-UNRELATED-HOST-KEY")
    container = service._client.containers.run.return_value
    container.id = "fake-container"
    container.attrs = {"NetworkSettings": {"Networks": {"artemis-net": {"IPAddress": "fake-ip"}}}}
    result = await service.start_artemis_container("synthetic", "fake-host", 1234)
    kwargs = service._client.containers.run.call_args.kwargs
    assert set(kwargs["environment"]) == {
        "SESSION_ID",
        "ADB_DEVICE_SERIAL",
        "PORT",
        "PYTHONUNBUFFERED",
        "ARTEMIS_ARTEMIS_JSONC",
        "ARTEMIS_APP_DIR",
        "ARTEMIS_CONFIG_READ_ONLY",
    }
    assert "SYNTHETIC-" not in repr(kwargs)
    assert len(kwargs["volumes"]) == 3
    assert all(value["mode"] == "ro" for value in kwargs["volumes"].values())
    assert all(key.startswith("/docker-host/configs/session-") for key in kwargs["volumes"])
    export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
    registry_file = export / "llm_providers.json"
    stored = json.loads(registry_file.read_text())
    assert {entry["name"] for entry in stored["providers"]} == {"mm-main", "mm-backup"}
    assert "SYNTHETIC-UNUSED-KEY" not in registry_file.read_text()
    assert (export / ".env").read_text() == "\n"
    main = export / "artemis.jsonc"
    assert "presets" not in json.loads(main.read_text())
    assert (
        json.loads(main.read_text())["agent"]
        .get("flash", {})
        .get("step_summarizer", {})
        .get("model")
        is None
    )
    for path in export.iterdir():
        if sys.platform != "win32":
            _verify_registry_file_permissions(path, None)
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(main))
    monkeypatch.setattr(
        providers, "provider_registry", providers.LLMProviderRegistry(registry_file)
    )
    prepared = prepare_task_submission("flash")
    assert {endpoint.registry_name for endpoint in prepared._llm_endpoint_snapshot.values()} == {
        "mm-main",
        "mm-backup",
    }
    assert result == {
        "container_id": "fake-container",
        "container_name": "artemis-session-synthetic",
        "internal_ip": "fake-ip",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("simulation", [True, False])
async def test_invalid_container_preflight_preserves_old_container(
    selected_config, docker_service, simulation
):
    service, _ = docker_service
    client = service._client
    if simulation:
        service._client = None
    main, data, _ = selected_config
    invalidate(main, data, "aux")
    with pytest.raises(ValueError):
        await service.start_artemis_container("synthetic", "fake-host", 1234)
    client.containers.get.assert_not_called()
    client.containers.run.assert_not_called()


@pytest.mark.parametrize("host_root", ["", "relative", "/docker/../escape", "C:relative"])
def test_container_requires_explicit_absolute_host_mapping(
    selected_config, docker_service, host_root
):
    service, config = docker_service
    config.ARTEMIS_EXPORT_HOST_ROOT = host_root
    with pytest.raises(ValueError, match="absolute ARTEMIS_EXPORT_HOST_ROOT"):
        service._container_configuration("flash")


@pytest.mark.asyncio
async def test_readonly_model_save_and_clear_fail_explicitly(selected_config, monkeypatch):
    from apps.admin_console.routers import system
    from artemis.config import settings
    from fastapi import HTTPException

    _, _, registry = selected_config
    before = registry.path.read_bytes()
    monkeypatch.setenv("ARTEMIS_CONFIG_READ_ONLY", "1")
    for action in (
        lambda: system.upsert_llm_provider(
            system.LLMProviderRequest(name="mm-main", api_key="", verify=False)
        ),
        lambda: system.delete_llm_provider("mm-main"),
        lambda: system.set_default_llm_provider("mm-backup"),
        lambda: system.update_credentials(
            system.UpdateCredentialsRequest(
                provider="google", api_key="", clear_api_key=True, confirm_clear=True
            )
        ),
    ):
        with pytest.raises(HTTPException) as error:
            await action()
        assert error.value.status_code == 403 and "read-only" in error.value.detail
    with pytest.raises(PermissionError, match="read-only"):
        settings.set_api_key("google", "", persist_to_env=False)
    with pytest.raises(PermissionError, match="read-only"):
        registry.remove("mm-main")
    assert registry.path.read_bytes() == before


def test_playground_images_reuse_root_runtime_and_file_injection():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    assert "FROM node:22-bookworm-slim AS frontend-builder" in dockerfile
    assert "./packages/artemis-client" in dockerfile
    for dependency in ("ffmpeg", "libgl1", "libglib2.0-0", "ca-certificates"):
        assert dependency in dockerfile
    for folder, port in (("artemis_container", 8080), ("backend_manager", 8000)):
        image = (root / "playground" / folder / "Dockerfile").read_text()
        compose = (root / "playground" / folder / "docker-compose.yml").read_text()
        assert "ARG ARTEMIS_BASE_IMAGE=artemis-runtime:latest" in image
        assert "FROM ${ARTEMIS_BASE_IMAGE}" in image
        assert "pip wheel" not in image and "pip install --no-cache-dir -e" not in image
        assert "context: ../.." in compose
        assert f"127.0.0.1:{port}:{port}" in compose
        assert "ARTEMIS_ARTEMIS_JSONC=/run/artemis/artemis.jsonc" in compose
        assert "GEMINI_API_KEY=${" not in compose and "OPENAI_API_KEY=${" not in compose


@pytest.mark.asyncio
@pytest.mark.parametrize("change_when", ["during_build", "after_export"])
async def test_container_source_change_is_rejected_before_replacement(
    selected_config, docker_service, monkeypatch, change_when
):
    from artemis.config import task as task_config

    main, _, _ = selected_config
    service, _ = docker_service
    client = service._client
    if change_when == "during_build":
        original = task_config.prepare_task_submission

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            main.write_bytes(main.read_bytes() + b" ")
            return result

        monkeypatch.setattr(task_config, "prepare_task_submission", changed)
    else:
        original = service._container_configuration

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            main.write_bytes(main.read_bytes() + b" ")
            return result

        monkeypatch.setattr(service, "_container_configuration", changed)
    with pytest.raises(ValueError, match="changed during container preflight"):
        await service.start_artemis_container("synthetic", "fake-host", 1234)
    client.containers.get.assert_not_called()
    client.containers.run.assert_not_called()


def test_container_legacy_independent_agent_file_preserves_implicit_aux(
    selected_config, docker_service, monkeypatch, tmp_path
):
    from artemis.config.task import prepare_task_submission
    from artemis.llm import providers

    main, data, _ = selected_config
    data.pop("agent")
    main.write_text(json.dumps(data))
    agent = tmp_path / "agent_config.json"
    agent.write_text(
        json.dumps(
            {
                "memory": {"runtime": {"retry_limit": 7}, "chunking": {"max_steps": 10}},
                "ocr": {"automatic": True},
                "flash": {"max_turns": 42},
            }
        )
    )
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
    monkeypatch.setenv("ARTEMIS_LLM_CONFIG_JSON", str(main))
    monkeypatch.setenv("ARTEMIS_AGENT_CONFIG_JSON", str(agent))
    original = prepare_task_submission("flash")
    service, config = docker_service
    service._container_configuration("flash")
    export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
    combined = export / "artemis.jsonc"
    saved = json.loads(combined.read_text())
    assert saved["agent"]["memory"]["runtime"]["retry_limit"] == 7
    assert saved["agent"]["ocr"]["automatic"] is True
    assert saved["agent"]["flash"]["max_turns"] == 42
    assert "model" not in saved["agent"]["memory"]["chunking"]
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(combined))
    monkeypatch.delenv("ARTEMIS_LLM_CONFIG_JSON")
    monkeypatch.delenv("ARTEMIS_AGENT_CONFIG_JSON")
    monkeypatch.setattr(
        providers, "provider_registry", providers.LLMProviderRegistry(export / "llm_providers.json")
    )
    loaded = prepare_task_submission("flash")
    assert loaded.agent_config.memory == original.agent_config.memory
    assert loaded.agent_config.ocr == original.agent_config.ocr
    assert loaded.agent_config.flash == original.agent_config.flash
    assert "model" not in loaded.agent_config.memory.chunking.model_fields_set
    assert "model" not in loaded.agent_config.flash.step_summarizer.model_fields_set
    assert loaded.model_snapshot == original.model_snapshot


@pytest.mark.asyncio
async def test_real_validator_catalog_only_on_actual_canonical_targets(
    selected_config, monkeypatch
):
    from artemis.utils import credentials_validator
    from mcp_server.tools.diagnose import _verify_credentials

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    # An actual httpx client + MockTransport exercises the real validator; no
    # paid inference or OCR operation and no real network are reachable.
    native_client = httpx.AsyncClient
    monkeypatch.setattr(native_client, "send", _HTTPX_SEND)
    monkeypatch.setattr(
        credentials_validator.httpx,
        "AsyncClient",
        lambda **kwargs: native_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = await _verify_credentials(None)
    assert all(entry["valid"] for entry in result)
    assert len(requests) == 2
    assert {request.method for request in requests} == {"GET"}
    assert {str(request.url) for request in requests} == {
        "https://one.invalid/anthropic/v1/models",
        "https://two.invalid/anthropic/v1/models",
    }
    assert {request.headers["x-api-key"] for request in requests} == {
        "SYNTHETIC-MAIN-KEY",
        "SYNTHETIC-BACKUP-KEY",
    }


@pytest.mark.asyncio
async def test_generated_export_lives_until_its_container_is_removed(
    selected_config, docker_service
):
    service, config = docker_service
    container = service._client.containers.run.return_value
    container.id = "owned-container-one"
    await service.start_artemis_container("synthetic", "fake-host", 1234)
    export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
    assert (export / "llm_providers.json").is_file()
    service._client.containers.get.return_value = container
    await service.stop_artemis_container("synthetic")
    container.remove.assert_called_once_with(force=True)
    assert not export.exists()
    assert selected_config[0].is_file() and selected_config[2].path.is_file()


@pytest.mark.asyncio
async def test_failed_container_start_cleans_only_generated_export(selected_config, docker_service):
    service, config = docker_service
    service._client.containers.run.side_effect = RuntimeError("synthetic start failure")
    with pytest.raises(RuntimeError, match="synthetic start failure"):
        await service.start_artemis_container("synthetic", "fake-host", 1234)
    assert list(config.ARTEMIS_EXPORT_ROOT.iterdir()) == []
    assert selected_config[0].is_file() and selected_config[2].path.is_file()


@pytest.mark.asyncio
async def test_failed_created_container_removal_retains_bind_files_and_original_error(
    selected_config, docker_service, caplog
):
    service, config = docker_service
    container = service._client.containers.run.return_value
    container.id = "owned-container-one"
    container.reload.side_effect = RuntimeError("synthetic reload failure")
    container.remove.side_effect = PermissionError("synthetic removal failure")
    with pytest.raises(RuntimeError, match="synthetic reload failure") as error:
        await service.start_artemis_container("synthetic", "fake-host", 1234)
    export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
    assert (export / "llm_providers.json").is_file()
    assert service._container_exports[container.id] == export
    assert any("configuration export retained" in note for note in error.value.__notes__)
    assert "configuration export retained" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_start_error(
    selected_config, docker_service, monkeypatch, caplog
):
    service, config = docker_service
    service._client.containers.run.side_effect = RuntimeError("synthetic start failure")
    monkeypatch.setattr(
        service,
        "_cleanup_export",
        MagicMock(side_effect=PermissionError("synthetic cleanup failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic start failure") as error:
        await service.start_artemis_container("synthetic", "fake-host", 1234)
    assert any("cleanup failed" in note for note in error.value.__notes__)
    assert "cleanup failed" in caplog.text
    assert len(list(config.ARTEMIS_EXPORT_ROOT.iterdir())) == 1


@pytest.mark.asyncio
async def test_old_generation_cleanup_cannot_delete_new_generation(selected_config, docker_service):
    service, config = docker_service
    one, two = MagicMock(id="container-one"), MagicMock(id="container-two")
    service._client.containers.run.side_effect = [one, two]
    await service.start_artemis_container("synthetic", "fake-host", 1234)
    old_export = service._container_exports[one.id]
    service._client.containers.get.return_value = one
    await service.start_artemis_container("synthetic", "fake-host", 1234)
    current_export = service._container_exports[two.id]
    assert not old_export.exists() and current_export.is_dir()
    with pytest.raises(ValueError, match="not owned"):
        service._cleanup_export(old_export)
    assert (current_export / "llm_providers.json").is_file()


@pytest.mark.asyncio
async def test_readiness_and_device_selection_public_boundaries_use_safe_reports(
    selected_config, monkeypatch
):
    from artemis.core.diagnostics.engine import ReadinessEngine
    from artemis.core.diagnostics.probes.credentials_probe import (
        LLMCredentialsProbe,
        VisionOCRProbe,
    )
    from apps.admin_console.routers import system

    engine = ReadinessEngine()
    engine._probes = {"gemini_api_key": LLMCredentialsProbe(), "vision_ocr_key": VisionOCRProbe()}
    monkeypatch.setattr(system, "readiness_engine", engine)
    public = await system.get_system_readiness()
    selected = await system.select_active_device(
        system.SelectDeviceRequest(serial="synthetic-device")
    )
    assert "SYNTHETIC" not in repr(public.model_dump())
    assert "SYNTHETIC" not in repr(selected)
    assert selected["report"].probes[0].metadata["verified"] is False


def test_requested_named_profile_uses_existing_canonical_selector(selected_config, monkeypatch):
    from artemis.config.task import prepare_task_submission
    from artemis.sdk.builders import Builders
    from artemis.sdk.types import AgentProfile
    from artemis.config import get_default_llm_config
    from third_party.mobile_use import main as worker_cli

    base = get_default_llm_config()
    builder = Builders.AgentConfig.with_default_profile(
        AgentProfile(name="default", llm_config=base)
    )
    named = base.model_copy(deep=True)
    named.default.model = "MiniMax-M3.1-Flash"
    builder.add_profile(AgentProfile(name="chosen", llm_config=named), validate=False)
    monkeypatch.setattr(worker_cli, "new_default_config_builder", lambda: builder)
    prepared = prepare_task_submission("chosen")
    assert prepared.selected_llm_profile == "chosen"
    assert prepared.model_snapshot["requested_profile"] == "chosen"
    assert prepared.llm_config.default.model == "MiniMax-M3.1-Flash"


def test_container_role_only_main_does_not_copy_unselected_presets_into_agent(
    selected_config, docker_service
):
    main, data, _ = selected_config
    data.pop("agent")
    main.write_text(json.dumps(data))
    service, config = docker_service
    service._container_configuration("flash")
    saved = json.loads(
        next(config.ARTEMIS_EXPORT_ROOT.iterdir()).joinpath("artemis.jsonc").read_text()
    )
    assert "presets" not in saved and "presets" not in saved["agent"]
    assert "default" not in saved["agent"]


@pytest.mark.asyncio
async def test_same_wire_auth_target_is_deduplicated_privately(selected_config, monkeypatch):
    from artemis.utils import credentials_validator
    from mcp_server.tools.diagnose import _verify_credentials

    _, _, registry = selected_config
    registry.add(
        name="mm-backup",
        api_key="SYNTHETIC-MAIN-KEY",
        api_base="https://one.invalid/anthropic",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3",
    )
    verify = AsyncMock(return_value=(True, "catalog authenticated"))
    monkeypatch.setattr(credentials_validator, "validate_api_key", verify)
    results = await _verify_credentials(None)
    verify.assert_awaited_once()
    assert len(results) == 1 and results[0]["valid"] is True
    assert set(results[0]) == {"provider", "label", "valid", "message"}
    assert "SYNTHETIC" not in repr(results)


@pytest.mark.asyncio
async def test_empty_or_invalid_canonical_targets_cannot_report_verified(
    selected_config, monkeypatch
):
    from mcp_server.tools.diagnose import _verify_credentials
    from mcp_server.utils import task_config

    monkeypatch.setattr(
        task_config, "prepare_task_config", lambda: SimpleNamespace(_llm_endpoint_snapshot={})
    )
    results = await _verify_credentials(None)
    assert results and results[0]["valid"] is False
    monkeypatch.setattr(
        task_config,
        "prepare_task_config",
        MagicMock(side_effect=ValueError("SYNTHETIC-ERROR-SECRET")),
    )
    results = await _verify_credentials(None)
    assert results and results[0]["valid"] is False
    assert "SYNTHETIC" not in repr(results)


@pytest.mark.asyncio
async def test_verification_orders_actual_default_before_role_overrides(
    selected_config, monkeypatch
):
    from artemis.llm.router import ModelEndpoint, ModelProvider
    from artemis.utils import credentials_validator
    from mcp_server.utils import task_config
    from mcp_server.tools.diagnose import _verify_credentials

    role = ModelEndpoint(provider=ModelProvider.OPENAI, api_key="SYNTHETIC-ROLE-KEY")
    default = ModelEndpoint(provider=ModelProvider.GOOGLE, api_key="SYNTHETIC-DEFAULT-KEY")
    monkeypatch.setattr(
        task_config,
        "prepare_task_config",
        lambda: SimpleNamespace(
            _llm_endpoint_snapshot={
                ("planner", False, False): role,
                ("default", False, False): default,
            }
        ),
    )
    monkeypatch.setattr(
        credentials_validator, "validate_api_key", AsyncMock(return_value=(True, "authenticated"))
    )
    results = await _verify_credentials(None)
    assert [result["provider"] for result in results] == ["google", "openai"]


@pytest.mark.parametrize(
    ("payload", "location", "error_type"),
    [
        (
            {"ocr": {"automatic": {"api_key": "SYNTHETIC-MISPLACED-CREDENTIAL"}}},
            ("ocr", "automatic"),
            "bool_type",
        ),
        (
            {
                "flash": {
                    "step_summarizer": {
                        "retry_limit": {"api_key": "SYNTHETIC-MISPLACED-CREDENTIAL"}
                    }
                }
            },
            ("flash", "step_summarizer", "retry_limit"),
            "int_type",
        ),
        (
            {"memory": {"chunking": {"max_steps": {"api_key": "SYNTHETIC-MISPLACED-CREDENTIAL"}}}},
            ("memory", "chunking", "max_steps"),
            "int_type",
        ),
    ],
)
def test_canonical_agent_config_keeps_nested_validation_without_input_echo(
    payload, location, error_type
):
    from artemis.config.agent import AgentGlobalConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        AgentGlobalConfig.model_validate(payload)
    errors = error.value.errors(include_input=False, include_context=False)
    assert any(tuple(item["loc"]) == location and item["type"] == error_type for item in errors)
    assert "SYNTHETIC-MISPLACED-CREDENTIAL" not in str(error.value)
    assert "input_value=" not in str(error.value)
    assert ".".join(location) in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_section", ["ocr", "memory"])
async def test_malformed_agent_input_is_hidden_in_actual_public_consumers(
    selected_config, monkeypatch, docker_service, caplog, invalid_section
):
    from artemis import runtime
    from artemis.core.diagnostics.engine import ReadinessEngine
    from artemis.core.diagnostics.probes.credentials_probe import (
        LLMCredentialsProbe,
        VisionOCRProbe,
    )
    from artemis.core.diagnostics.schema import ProbeStatus
    from apps.admin_console.routers import tasks
    from apps.admin_console.schemas.task_schema import RunRequest
    from apps.admin_console.services.task_queue_service import TaskQueueService
    from fastapi import HTTPException
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server.base import mcp
    from mcp_server.tools import task_runner

    main, data, _ = selected_config
    secret_input = {"api_key": "SYNTHETIC-MISPLACED-CREDENTIAL"}
    data["agent"].update(
        {"ocr": {"automatic": secret_input}}
        if invalid_section == "ocr"
        else {"memory": {"chunking": {"max_steps": secret_input}}}
    )
    main.write_text(json.dumps(data), encoding="utf-8")
    engine = ReadinessEngine()
    engine._probes = {"gemini_api_key": LLMCredentialsProbe(), "vision_ocr_key": VisionOCRProbe()}
    first = await engine.run_all()
    cached = await engine.run_all()
    assert (
        next(probe for probe in first.probes if probe.id == "vision_ocr_key").status
        == ProbeStatus.FAIL
    )
    assert first.model_dump() == cached.model_dump()
    messages = {"first": repr(first.model_dump()), "cached": repr(cached.model_dump())}
    fail = MagicMock(side_effect=AssertionError("side effect before preflight"))
    device = AsyncMock(side_effect=AssertionError("device before preflight"))
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", fail)
    monkeypatch.setattr(TaskQueueService, "_create_queue_item", fail)
    monkeypatch.setattr(TaskQueueService, "_find_duplicate_submission", lambda *args: None)
    monkeypatch.setattr(tasks.device_pool, "validate_explicit_serial_async", device)
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", device)
    monkeypatch.setattr(runtime.trace_store, "init_trace", fail)
    monkeypatch.setattr(runtime, "ensure_daemon_running", fail)
    monkeypatch.setattr(runtime.DeviceExecutionLock, "reserve", fail)
    monkeypatch.setattr(task_runner, "_validate_device_serial", fail)
    with pytest.raises(HTTPException) as api_error:
        await tasks.run_task(RunRequest(goal="synthetic", profile="flash"))
    assert api_error.value.status_code == 422
    messages["api"] = api_error.value.detail
    with pytest.raises(ValueError) as queue_error:
        await TaskQueueService.enqueue_tasks(["synthetic"], profile="flash")
    messages["queue"] = str(queue_error.value)
    # Exercise the registered FastMCP tool boundary, not just the Python function.
    with pytest.raises(ToolError) as mcp_error:
        await mcp.call_tool("mobile_run_task", {"task_desc": "synthetic", "model": "Flash"})
    messages["mcp"] = str(mcp_error.value)
    service, _ = docker_service
    with pytest.raises(ValueError) as container_error:
        service._container_configuration("flash")
    messages["container"] = str(container_error.value)
    assert all("SYNTHETIC-MISPLACED-CREDENTIAL" not in message for message in messages.values())
    assert "SYNTHETIC-MISPLACED-CREDENTIAL" not in caplog.text
    service._client.containers.get.assert_not_called()
    service._client.containers.run.assert_not_called()
    fail.assert_not_called()
    device.assert_not_called()


@pytest.mark.parametrize("provider", ["custom", "ollama", "vllm"])
@pytest.mark.parametrize("authenticated", [True, False])
def test_selected_custom_transport_auth_roundtrips_minimally(
    selected_config, monkeypatch, docker_service, provider, authenticated
):
    from artemis.config import settings
    from artemis.config.llm import OPTIONAL_AGENT_ROLES
    from artemis.config.task import prepare_task_submission
    from artemis.llm import providers
    from dotenv import dotenv_values

    main, _, _ = selected_config
    role = {
        "provider": provider,
        "model": "private-model",
        "fallback": {"provider": provider, "model": "private-model"},
    }
    data = {
        "default": role,
        "nodes": {name: role for name in OPTIONAL_AGENT_ROLES},
        "agent": {"ocr": {"automatic": False}},
    }
    main.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: None)
    if authenticated:
        monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC-CUSTOM-AUTH-KEY")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC-UNRELATED-HOST-KEY")
    prepared = prepare_task_submission("flash")
    expected_key = "SYNTHETIC-CUSTOM-AUTH-KEY" if authenticated else "EMPTY"
    assert {endpoint.api_key for endpoint in prepared._llm_endpoint_snapshot.values()} == {
        expected_key
    }
    service, config = docker_service
    environment, volumes, _, _ = service._container_configuration("flash")
    exported = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
    values = dotenv_values(exported / ".env")
    assert set(values) == (
        {"OPENAI_API_KEY", "OPENAI_BASE_URL"} if authenticated else {"OPENAI_BASE_URL"}
    )
    assert "SYNTHETIC-UNRELATED-HOST-KEY" not in (exported / ".env").read_text()
    assert "SYNTHETIC" not in repr(environment)
    assert all(mount["mode"] == "ro" for mount in volumes.values())
    assert json.loads((exported / "llm_providers.json").read_text())["providers"] == []
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(exported / "artemis.jsonc"))
    monkeypatch.setattr(
        providers,
        "provider_registry",
        providers.LLMProviderRegistry(exported / "llm_providers.json"),
    )
    loaded = prepare_task_submission("flash")
    assert {endpoint.api_key for endpoint in loaded._llm_endpoint_snapshot.values()} == {
        expected_key
    }
    assert {endpoint.api_base for endpoint in loaded._llm_endpoint_snapshot.values()} == {
        "https://custom.invalid/v1"
    }
    assert loaded.model_snapshot == prepared.model_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["late_failure", "late_success", "remove_failure", "cleanup_failure"]
)
async def test_cancelled_container_start_settles_worker_and_repeated_cleanup_cancellation(
    selected_config, docker_service, monkeypatch, caplog, outcome
):
    service, config = docker_service
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    removing, release_remove = threading.Event(), threading.Event()
    container = service._client.containers.run.return_value
    container.id = "synthetic-cancelled-container"

    def delayed_run(**kwargs):
        entered.set()
        try:
            assert release.wait(5), "test must release its owned Docker worker"
            if outcome in {"late_failure", "cleanup_failure"}:
                raise RuntimeError("synthetic delayed start failure")
            return container
        finally:
            finished.set()

    def delayed_remove(**kwargs):
        removing.set()
        assert release_remove.wait(5), "test must release its owned removal worker"
        if outcome == "remove_failure":
            raise PermissionError("synthetic removal failure")

    service._client.containers.run.side_effect = delayed_run
    container.remove.side_effect = delayed_remove
    if outcome == "cleanup_failure":
        monkeypatch.setattr(
            service,
            "_cleanup_export",
            MagicMock(side_effect=PermissionError("synthetic cleanup failure")),
        )
    task = asyncio.create_task(service.start_artemis_container("cancelled", "fake-host", 1234))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
        task.cancel("original cancellation")
        await asyncio.sleep(0)
        assert not task.done(), "caller must retain ownership until the worker actually settles"
        task.cancel("repeat while Docker runs")
        await asyncio.sleep(0)
        assert not task.done()
        assert (export / "llm_providers.json").is_file()
        release.set()
        if outcome in {"late_success", "remove_failure"}:
            assert await asyncio.to_thread(removing.wait, 5)
            assert (export / "llm_providers.json").is_file()
            task.cancel("repeat during owned removal")
            await asyncio.sleep(0)
            assert not task.done(), "repeated cancellation must not detach owned removal"
            release_remove.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation") as error:
            await task
        assert finished.is_set()
        if outcome in {"late_failure", "late_success"}:
            assert not export.exists()
            assert not service._owned_exports and not service._container_exports
        elif outcome == "remove_failure":
            assert (export / "llm_providers.json").is_file()
            assert service._container_exports[container.id] == export
            assert any("configuration export retained" in note for note in error.value.__notes__)
            assert "configuration export retained" in caplog.text
        else:
            assert (export / "llm_providers.json").is_file()
            assert export in service._owned_exports and not service._container_exports
            assert any("cleanup failed" in note for note in error.value.__notes__)
            assert "cleanup failed" in caplog.text
        if outcome in {"late_success", "remove_failure"}:
            container.remove.assert_called_once_with(force=True)
        else:
            container.remove.assert_not_called()
            assert any(
                "worker failed after cancellation: RuntimeError" in note
                for note in error.value.__notes__
            )
    finally:
        release.set()
        release_remove.set()
        if not task.done():
            task.cancel("test cleanup")
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
        except (asyncio.CancelledError, RuntimeError, PermissionError):
            # The expected task outcome was asserted above; all events are now
            # released so a failing test cannot leave an executor blocked.
            assert task.done()
        assert await asyncio.to_thread(finished.wait, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_worker_failure_is_preserved_when_caller_cancels_during_cleanup(
    selected_config, docker_service, monkeypatch, caplog, cleanup_fails
):
    service, config = docker_service
    container = service._client.containers.run.return_value
    container.id = "synthetic-failed-container"
    container.reload.side_effect = RuntimeError("original worker failure")
    removing, release_remove, removed = threading.Event(), threading.Event(), threading.Event()

    def delayed_remove(**kwargs):
        removing.set()
        assert release_remove.wait(5), "test must release its owned removal worker"
        removed.set()

    container.remove.side_effect = delayed_remove
    if cleanup_fails:
        monkeypatch.setattr(
            service,
            "_cleanup_export",
            MagicMock(side_effect=PermissionError("synthetic cleanup failure")),
        )
    task = asyncio.create_task(
        service.start_artemis_container("cancelled-cleanup", "fake-host", 1234)
    )
    try:
        assert await asyncio.to_thread(removing.wait, 5)
        export = next(config.ARTEMIS_EXPORT_ROOT.iterdir())
        task.cancel("cancel during cleanup")
        await asyncio.sleep(0)
        task.cancel("repeat during cleanup")
        await asyncio.sleep(0)
        assert not task.done()
        assert (export / "llm_providers.json").is_file()
        release_remove.set()
        with pytest.raises(RuntimeError, match="original worker failure") as error:
            await task
        assert removed.is_set()
        assert not service._container_exports
        if cleanup_fails:
            assert export in service._owned_exports
            assert (export / "llm_providers.json").is_file()
            assert any("cleanup failed" in note for note in error.value.__notes__)
            assert "cleanup failed" in caplog.text
        else:
            assert not export.exists() and not service._owned_exports
    finally:
        release_remove.set()
        if not task.done():
            task.cancel("test cleanup")
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
        except (asyncio.CancelledError, RuntimeError, PermissionError):
            assert task.done()
        assert await asyncio.to_thread(removed.wait, 5)
