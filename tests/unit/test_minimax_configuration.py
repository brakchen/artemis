"""D2 configuration boundaries through real HTTP, CLI and offline SDK clients."""

from __future__ import annotations

import asyncio
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import HumanMessage
import pytest
from typer.testing import CliRunner

from apps.admin_console.server import app
from artemis.config.agent import AgentGlobalConfig
from artemis.config.llm import AGENT_ROLES, UTILITY_ROLES, LLMConfig, _expand_default_into_nodes
from artemis.config.task import prepare_selected_task_config
from artemis.interfaces.cli.commands import init as setup
from artemis.interfaces.cli.commands import providers as cli_providers
from artemis.interfaces.cli.commands import run as cli_run
from artemis.interfaces.cli.main import app as cli
from artemis.llm import providers
from artemis.llm.providers import LLMProviderRegistry, build_models_list_url, discover_models
from artemis.llm.router import ModelFactory
from artemis.utils.credentials_validator import validate_api_key
from third_party.mobile_use.utils.file import load_jsonc

KEY = "offline-sentinel-secret-0123456789"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "registry.json")
    monkeypatch.setattr(providers, "provider_registry", registry)
    monkeypatch.setattr(cli_providers, "provider_registry", registry)
    return registry


def saved(registry):
    return registry.add(
        name="mini",
        api_key=KEY,
        kind="anthropic",
        vendor="minimax",
        api_base="https://mini.invalid/anthropic",
        model="MiniMax-M3.1-Flash",
        fallback_model="MiniMax-M3",
        temperature=0.3,
        timeout_seconds=37,
        is_multimodal=False,
        enabled=False,
    )


def mock_http(monkeypatch, handler):
    original = AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**{"transport": httpx.MockTransport(handler), **kwargs}),
    )


@pytest.mark.asyncio
async def test_http_edit_preserves_complete_candidate_and_secret(registry, monkeypatch):
    before = saved(registry)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "MiniMax-M3.1-Flash"}]})

    mock_http(monkeypatch, handle)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        for route in ("test", "models"):
            response = await client.post(
                f"/api/system/llm-providers/{route}", json={"name": "mini", "api_key": ""}
            )
            assert response.status_code == 200, response.text
            assert KEY not in response.text
        response = await client.post(
            "/api/system/llm-providers", json={"name": "mini", "api_key": ""}
        )
        assert response.status_code == 200, response.text
    assert registry.get("mini") == before
    assert len(requests) == 3
    assert all(
        str(request.url) == "https://mini.invalid/anthropic/v1/models" for request in requests
    )
    assert all(request.headers["x-api-key"] == KEY for request in requests)
    assert all("authorization" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_explicit_draft_protocol_overrides_stored_metadata(registry, monkeypatch):
    saved(registry)
    seen = []
    mock_http(
        monkeypatch,
        lambda request: (
            seen.append(request) or httpx.Response(200, json={"data": [{"id": "manual-model"}]})
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        for route in ("test", "models"):
            response = await client.post(
                f"/api/system/llm-providers/{route}",
                json={
                    "name": "mini",
                    "kind": "openai",
                    "vendor": None,
                    "api_base": "https://open.invalid/v1",
                },
            )
            assert response.status_code == 200, response.text
    assert all(request.headers["authorization"] == f"Bearer {KEY}" for request in seen)
    assert all("x-api-key" not in request.headers for request in seen)
    assert registry.get("mini").kind == "anthropic"


@pytest.mark.parametrize(
    "base",
    [
        "https://api.minimax.cn",
        "https://api.minimax.cn/v1",
        "https://api.minimax.cn/anthropic",
        "https://api.minimax.cn/anthropic/v1",
        "https://api.minimax.cn/anthropic/v1/models",
    ],
)
def test_minimax_catalog_base_forms(base):
    assert (
        build_models_list_url(base, kind="anthropic", vendor="minimax")
        == "https://api.minimax.cn/anthropic/v1/models"
    )


@pytest.mark.asyncio
async def test_actual_validation_422_never_echoes_nested_secret(registry):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/system/llm-providers",
            json={
                "name": "mini",
                "api_key": {"nested": [KEY]},
                "timeout_seconds": {"credential": KEY},
            },
        )
    assert response.status_code == 422
    assert KEY not in response.text
    assert "input" not in response.text and "ctx" not in response.text
    assert {tuple(item["loc"]) for item in response.json()["detail"]} == {
        ("body", "api_key"),
        ("body", "timeout_seconds"),
    }


@pytest.mark.asyncio
async def test_upstream_echo_and_transport_exception_redacted(registry, monkeypatch):
    saved(registry)
    mock_http(
        monkeypatch, lambda _: httpx.Response(401, json={"error": {"message": f"rejected {KEY}"}})
    )
    valid, message = await validate_api_key(
        "anthropic", KEY, "https://mini.invalid/anthropic", vendor="minimax"
    )
    assert not valid and KEY not in message and "401" in message
    with pytest.raises(RuntimeError, match="401") as error:
        await discover_models("https://mini.invalid/anthropic", KEY, kind="anthropic")
    assert KEY not in str(error.value)

    def fail(request):
        raise httpx.ConnectError(KEY, request=request)

    mock_http(monkeypatch, fail)
    valid, message = await validate_api_key("anthropic", KEY)
    assert not valid and KEY not in message
    with pytest.raises(RuntimeError) as error:
        await discover_models("https://mini.invalid/anthropic", KEY, kind="anthropic")
    assert KEY not in str(error.value)


@pytest.mark.asyncio
async def test_unknown_vendor_never_passes_key_length(registry):
    assert (await validate_api_key("unknown-vendor", KEY))[0] is False
    assert (await validate_api_key("anthropic", KEY, vendor="unknown"))[0] is False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/system/llm-providers/test",
            json={"name": "new", "api_key": KEY, "kind": "anthropic", "vendor": "unknown"},
        )
    assert response.status_code == 400 and KEY not in response.text


@pytest.mark.asyncio
async def test_explicit_clear_requires_confirmation_and_does_not_implicitly_disable(registry):
    registry.add(name="mini", api_key=KEY, enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/system/llm-providers", json={"name": "mini", "clear_api_key": True}
        )
        assert response.status_code == 400
        assert registry.get("mini").api_key.get_secret_value() == KEY
        response = await client.post(
            "/api/system/llm-providers",
            json={
                "name": "mini",
                "clear_api_key": True,
                "confirm_clear": True,
                "api_key": "conflicting-secret",
            },
        )
        assert response.status_code == 400
        response = await client.post(
            "/api/system/llm-providers",
            json={"name": "mini", "clear_api_key": True, "confirm_clear": True},
        )
        assert response.status_code == 200
    assert registry.get("mini").api_key.get_secret_value() == ""
    assert registry.get("mini").enabled is True


@pytest.mark.asyncio
async def test_legacy_blank_save_keeps_key_and_clear_is_guarded(monkeypatch):
    from artemis.config import settings
    from apps.admin_console.routers import system

    setter = Mock()
    monkeypatch.setattr(type(settings), "set_api_key", setter)
    monkeypatch.setattr(
        system.readiness_engine,
        "run_all",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
            return_value={"overall_ready": False}
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/system/credentials", json={"api_key": ""})
        assert response.status_code == 200
        setter.assert_not_called()
        response = await client.post(
            "/api/system/credentials", json={"api_key": "", "clear_api_key": True}
        )
        assert response.status_code == 400
        setter.assert_not_called()
        response = await client.post(
            "/api/system/credentials",
            json={"api_key": "", "clear_api_key": True, "confirm_clear": True},
        )
        assert response.status_code == 200
        setter.assert_called_once()


def test_cli_key_only_edit_preserves_metadata_and_clear_cancel(registry):
    before = saved(registry)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["providers", "edit", "--name", "mini", "--api-key", "new-offline-secret", "--no-test"]
    )
    assert result.exit_code == 0, result.output
    assert "new-offline-secret" not in result.output
    after = registry.get("mini")
    assert after.model_dump(exclude={"api_key"}) == before.model_dump(exclude={"api_key"})
    assert after.api_key.get_secret_value() == "new-offline-secret"
    result = runner.invoke(
        cli, ["providers", "edit", "--name", "mini", "--clear-api-key"], input="n\n"
    )
    assert result.exit_code != 0
    assert registry.get("mini") == after
    result = runner.invoke(
        cli, ["providers", "edit", "--name", "mini", "--clear-api-key", "--disabled", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert not registry.get("mini").enabled
    assert registry.get("mini").api_key.get_secret_value() == ""


@pytest.fixture
def config_files(tmp_path, monkeypatch, registry):
    path = tmp_path / "selected.jsonc"
    source = Path("config/artemis.jsonc").read_text(encoding="utf-8")
    data = load_jsonc(StringIO(source))
    data["device"] = {"keep": "offline-device"}
    data["agent"]["flash"]["step_summarizer"]["retry_limit"] = 7
    data["agent"]["memory"]["chunking"]["max_steps"] = 19
    path.write_text(json.dumps(data), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        "UNRELATED=keep\nADB_DEVICE_SERIAL=untouched\nARTEMIS_DEFAULT_PROFILE=flash\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "get_env_file", lambda: env)
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(path))
    return path, env, data


@pytest.mark.parametrize("model", ["MiniMax-M3.1-Flash", "MiniMax-M3"])
@pytest.mark.asyncio
async def test_init_migration_reaches_all_canonical_endpoints_and_actual_sdk(
    config_files, registry, model
):
    path, env, before = config_files
    target, backups = setup.migrate_minimax(
        name="mini", api_key=KEY, base_url="https://mini.invalid/anthropic", model=model
    )
    assert target == path
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["presets"] == before["presets"] and after["device"] == before["device"]
    assert set(after["nodes"]) == set(AGENT_ROLES) | set(UTILITY_ROLES)
    assert "UNRELATED=keep" in env.read_text() and "ADB_DEVICE_SERIAL=untouched" in env.read_text()
    assert (
        "GEMINI_API_KEY" not in env.read_text() and "ARTEMIS_DEFAULT_MODEL" not in env.read_text()
    )
    assert len(backups) == 2 and all(backup.is_file() for _, backup in backups)
    assert after["agent"]["flash"]["step_summarizer"]["retry_limit"] == 7
    assert after["agent"]["memory"]["chunking"]["max_steps"] == 19
    prepared = prepare_selected_task_config(
        AgentGlobalConfig.model_validate(after["agent"]),
        SimpleNamespace(
            name="default", llm_config=LLMConfig.model_validate(_expand_default_into_nodes(after))
        ),
    )
    endpoints = prepared._llm_endpoint_snapshot
    assert {endpoint.vendor for endpoint in endpoints.values()} == {"minimax"}
    assert {endpoint.model_name for endpoint in endpoints.values()} == {model}
    assert all((role, False, True) in endpoints for role in AGENT_ROLES)
    assert all((role, True, True) in endpoints for role in UTILITY_ROLES)
    assert ("step_summarizer", False, False) in endpoints and (
        "memory_chunking",
        False,
        True,
    ) in endpoints
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async with AsyncClient(transport=httpx.MockTransport(handle)) as client:
        endpoint = endpoints[("operator", False, False)]
        actual = ModelFactory.create_model(endpoint)
        actual._async_client = anthropic.AsyncAnthropic(
            api_key=endpoint.api_key, base_url=endpoint.api_base, http_client=client, max_retries=0
        )
        await actual.ainvoke([HumanMessage(content="offline")])
    payload = json.loads(seen[0].content)
    assert seen[0].url.path == "/anthropic/v1/messages"
    assert seen[0].headers["x-api-key"] == KEY
    assert payload["model"] == model
    assert payload["thinking"] == {"type": "adaptive" if model.endswith("Flash") else "disabled"}
    if model.endswith("Flash"):
        assert payload["output_config"]["effort"] == "medium"
    else:
        assert "output_config" not in payload


def test_invalid_migration_never_publishes_and_existing_key_is_preserved(config_files, registry):
    path, env, _ = config_files
    saved(registry)
    originals = {p: p.read_bytes() for p in (path, env, registry.path)}
    with pytest.raises(ValueError):
        setup.migrate_minimax(name="mini", api_key=None, base_url=None, model="future-model")
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert not list(path.parent.glob("*.backup-*"))
    setup.migrate_minimax(name="mini", api_key="", base_url=None, model="MiniMax-M3.1-Flash")
    assert registry.get("mini").api_key.get_secret_value() == KEY


@pytest.mark.parametrize("later_edit", [False, True])
def test_init_conditional_rollback_only_own_writes(config_files, registry, monkeypatch, later_edit):
    path, env, _ = config_files
    saved(registry)
    originals = {p: p.read_bytes() for p in (path, env, registry.path)}
    write = setup._protected_write

    def fail(target, content):
        if target == env:
            if later_edit:
                path.write_text("later user edit", encoding="utf-8")
            raise OSError("offline injected failure")
        write(target, content)

    monkeypatch.setattr(setup, "_protected_write", fail)
    with pytest.raises(OSError):
        setup.migrate_minimax(
            name="mini", api_key="replacement", base_url=None, model="MiniMax-M3.1-Flash"
        )
    assert registry.path.read_bytes() == originals[registry.path]
    assert env.read_bytes() == originals[env]
    assert path.read_bytes() == (b"later user edit" if later_edit else originals[path])
    assert len(list(path.parent.glob("*.backup-*"))) == 3


def test_cli_parent_preflight_before_any_daemon_or_device_effect(
    config_files, registry, monkeypatch
):
    path, _, _ = config_files
    setup.migrate_minimax(
        name="mini",
        api_key=KEY,
        base_url="https://mini.invalid/anthropic",
        model="MiniMax-M3.1-Flash",
    )
    data = json.loads(path.read_text())
    data["agent"]["memory"]["chunking"]["model"] = "gemini-explicit-unsupported"
    path.write_text(json.dumps(data), encoding="utf-8")
    import artemis.runtime as runtime

    forbidden = Mock(side_effect=AssertionError("device/daemon side effect before preflight"))
    monkeypatch.setattr(runtime, "ensure_daemon_running", forbidden)
    monkeypatch.setattr(runtime, "submit_task_to_daemon", forbidden)
    monkeypatch.setattr(runtime.device_pool, "select_device", forbidden)
    monkeypatch.setattr(cli_run, "display_local_device_status", forbidden)
    monkeypatch.setattr(cli_run, "run_automation", forbidden)
    result = CliRunner().invoke(cli, ["run", "offline task", "--profile", "flash"])
    assert result.exit_code == 1, result.output
    assert "preflight failed" in result.output and "init --provider minimax" in result.output
    assert "provider: mini" in result.output
    assert "GEMINI_API_KEY" not in result.output and KEY not in result.output
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "placeholder", ["", "API_KEY", "your_minimax_api_key_here", "<MINIMAX_API_KEY>"]
)
def test_init_rejects_placeholder_before_publishing(config_files, registry, placeholder):
    path, env, _ = config_files
    originals = {p: p.read_bytes() for p in (path, env)}
    with pytest.raises(ValueError):
        setup.migrate_minimax(
            name="mini",
            api_key=placeholder,
            base_url="https://mini.invalid/anthropic",
            model="MiniMax-M3.1-Flash",
        )
    assert not registry.path.exists()
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert not list(path.parent.glob("*.backup-*"))


def test_cli_explicit_env_import_preserves_conflict_unless_replacement_requested(
    config_files, registry, monkeypatch
):
    saved(registry)
    imported = "different-process-bootstrap-secret"
    monkeypatch.setenv("MINIMAX_API_KEY", imported)
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--provider", "minimax", "--name", "mini", "--import-env"])
    assert result.exit_code == 0, result.output
    assert registry.get("mini").api_key.get_secret_value() == KEY
    assert registry.get("mini").api_base == "https://api.minimax.cn/anthropic"
    result = runner.invoke(
        cli, ["init", "--provider", "minimax", "--name", "mini", "--import-env", "--replace-key"]
    )
    assert result.exit_code == 0, result.output
    assert registry.get("mini").api_key.get_secret_value() == imported
    assert KEY not in result.output and imported not in result.output
    assert "Protected backup:" in result.output and "Rollback:" in result.output


def test_cli_never_imports_global_google_or_anthropic_keys(config_files, registry, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-anthropic-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "wrong-google-secret")
    result = CliRunner().invoke(cli, ["init", "--provider", "minimax", "--import-env"])
    assert result.exit_code == 1
    assert not registry.path.exists()
    assert "MINIMAX_API_KEY is missing" in result.output
    assert (
        "wrong-anthropic-secret" not in result.output and "wrong-google-secret" not in result.output
    )


def test_wheel_migration_writes_user_copy_and_preserves_template(
    config_files, registry, monkeypatch, tmp_path
):
    from artemis.config import paths

    template, env, _ = config_files
    original = template.read_bytes()
    app_dir = tmp_path / "user-app"
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSON", raising=False)
    monkeypatch.setattr(paths, "get_main_config_path", lambda _: template)
    monkeypatch.setattr(paths, "is_source_checkout", lambda: False)
    monkeypatch.setattr(paths, "get_app_dir", lambda: app_dir)
    target, backups = setup.migrate_minimax(
        name="mini",
        api_key=KEY,
        base_url="https://mini.invalid/anthropic",
        model="MiniMax-M3.1-Flash",
    )
    assert target == app_dir / "artemis.jsonc"
    assert target.exists() and template.read_bytes() == original
    assert str(target.resolve()).replace("\\", "\\\\") in env.read_text()
    assert all(original_path != template for original_path, _ in backups)


def test_candidate_save_remerges_hidden_metadata_under_lock(registry):
    saved(registry)
    draft = registry.candidate(name="mini", api_key="candidate-key")
    registry.add(
        **{**registry.get("mini").to_storage_dict(), "temperature": 0.8, "timeout_seconds": 99}
    )
    actual = registry.save_candidate(name="mini", api_key=draft.api_key.get_secret_value())
    assert actual.temperature == 0.8 and actual.timeout_seconds == 99
    assert actual.vendor == "minimax" and actual.kind == "anthropic" and actual.enabled is False


@pytest.mark.asyncio
async def test_http_blank_edit_never_replays_stale_secret_after_verification(registry, monkeypatch):
    from artemis.utils import credentials_validator

    saved(registry)

    async def verify_then_concurrent_edit(**kwargs):
        assert kwargs["api_key"] == KEY
        registry.add(
            **{
                **registry.get("mini").to_storage_dict(),
                "api_key": "later-user-secret",
                "temperature": 0.9,
                "timeout_seconds": 91,
            }
        )
        return True, "Catalog authentication verified"

    monkeypatch.setattr(credentials_validator, "validate_api_key", verify_then_concurrent_edit)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/system/llm-providers", json={"name": "mini", "api_key": ""}
        )
    assert response.status_code == 200
    actual = registry.get("mini")
    assert actual.api_key.get_secret_value() == "later-user-secret"
    assert actual.temperature == 0.9 and actual.timeout_seconds == 91
    assert actual.kind == "anthropic" and actual.vendor == "minimax" and actual.enabled is False


def test_cli_explicit_generic_vendor_overrides_stored_minimax(registry):
    saved(registry)
    result = CliRunner().invoke(
        cli,
        [
            "providers",
            "edit",
            "--name",
            "mini",
            "--kind",
            "openai",
            "--vendor",
            "generic",
            "--base-url",
            "https://other.invalid/v1",
            "--no-test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert registry.get("mini").kind == "openai" and registry.get("mini").vendor is None
    assert registry.get("mini").api_key.get_secret_value() == KEY


@pytest.mark.parametrize("command", ["test", "models"])
def test_cli_test_and_catalog_use_explicit_complete_protocol_draft(registry, monkeypatch, command):
    saved(registry)
    seen = []
    mock_http(
        monkeypatch,
        lambda request: (
            seen.append(request) or httpx.Response(200, json={"data": [{"id": "manual-id"}]})
        ),
    )
    result = CliRunner().invoke(
        cli,
        [
            "providers",
            command,
            "mini",
            "--kind",
            "openai",
            "--vendor",
            "generic",
            "--base-url",
            "https://other.invalid/v1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert len(seen) == 1 and seen[0].url.path == "/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert registry.get("mini").kind == "anthropic"


@pytest.mark.parametrize(
    "choice,env_key",
    [
        ("1", "GEMINI_API_KEY"),
        ("2", "OPENAI_API_KEY"),
        ("3", "ANTHROPIC_API_KEY"),
        ("4", "OPEN_ROUTER_API_KEY"),
    ],
)
def test_legacy_init_choices_preserve_unrelated_env_and_runtime(
    config_files, monkeypatch, choice, env_key
):
    from dotenv import dotenv_values

    _, env, _ = config_files
    answers = iter([choice, KEY, "8"])
    monkeypatch.setattr(setup.Prompt, "ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    values = dotenv_values(env)
    assert values[env_key] == KEY
    assert values["UNRELATED"] == "keep" and values["ADB_DEVICE_SERIAL"] == "untouched"
    assert values["ARTEMIS_DEFAULT_PROFILE"] == "flash"
    assert "ARTEMIS_DEFAULT_MODEL" not in values
    assert KEY not in result.output


@pytest.mark.asyncio
async def test_minimax_without_base_never_sends_key_to_anthropic_default(registry, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("No HTTP client may be constructed"))
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    valid, message = await validate_api_key("anthropic", KEY, vendor="minimax")
    assert not valid and "explicit" in message
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/system/llm-providers",
            json={"name": "mini", "api_key": KEY, "kind": "anthropic", "vendor": "minimax"},
        )
    assert response.status_code == 400
    assert not registry.path.exists()
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_runtime_transport_error_and_chain_are_safe_at_library_http_cli_boundaries(
    registry, monkeypatch, caplog
):
    import traceback

    saved(registry)

    def fail(request):
        try:
            raise ValueError(f"untrusted body containing {KEY}")
        except ValueError as cause:
            raise RuntimeError(f"untrusted transport header {KEY}") from cause

    mock_http(monkeypatch, fail)
    with pytest.raises(RuntimeError, match="Could not reach the provider model catalog") as error:
        await discover_models("https://mini.invalid/anthropic", KEY, kind="anthropic")
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert KEY not in "".join(traceback.format_exception(error.value))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/system/llm-providers/models", json={"name": "mini"})
    assert response.status_code == 502 and KEY not in response.text
    result = await asyncio.to_thread(CliRunner().invoke, cli, ["providers", "models", "mini"])
    assert result.exit_code == 1 and KEY not in result.output
    assert "Could not reach the provider model catalog" in result.output
    assert KEY not in caplog.text


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "imported",
    [None, "", "   ", "\t\r\n", "API_KEY", "your_minimax_api_key_here", "<MINIMAX_API_KEY>"],
)
def test_explicit_import_rejects_empty_and_template_source_before_any_publish(
    config_files, registry, monkeypatch, existing, imported
):
    path, env, _ = config_files
    if existing:
        saved(registry)
    originals = {p: p.read_bytes() if p.exists() else None for p in (path, env, registry.path)}
    if imported is None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MINIMAX_API_KEY", imported)
    arguments = ["init", "--provider", "minimax", "--name", "mini", "--import-env"]
    if existing:
        arguments.append("--replace-key")
    result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 1, result.output
    assert {p: p.read_bytes() if p.exists() else None for p in originals} == originals
    assert not list(path.parent.glob("*.backup-*"))
    assert KEY not in result.output
    if imported and imported.strip() and imported != "API_KEY":
        assert imported not in result.output


def test_manual_blank_key_keeps_stored_key_after_explicit_import_validation(
    config_files, registry, monkeypatch
):
    saved(registry)
    monkeypatch.setattr(setup.Prompt, "ask", lambda *args, **kwargs: "")
    result = CliRunner().invoke(cli, ["init", "--provider", "minimax", "--name", "mini"])
    assert result.exit_code == 0, result.output
    assert registry.get("mini").api_key.get_secret_value() == KEY


@pytest.mark.parametrize(
    "layout,edit,existing_target",
    [
        ("selected", "source", True),
        ("wheel", "source", False),
        ("wheel", "target", False),
        ("wheel", "target", True),
    ],
)
def test_edit_after_source_read_rejects_stale_candidate_before_any_backup_or_publish(
    config_files, registry, monkeypatch, tmp_path, layout, edit, existing_target
):
    from artemis.config import paths

    source, env, data = config_files
    saved(registry)
    target = source
    if layout == "wheel":
        app_dir = tmp_path / "user-copy"
        target = app_dir / "artemis.jsonc"
        monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
        monkeypatch.delenv("ARTEMIS_ARTEMIS_JSON", raising=False)
        monkeypatch.setattr(paths, "get_main_config_path", lambda _: source)
        monkeypatch.setattr(paths, "is_source_checkout", lambda: False)
        monkeypatch.setattr(paths, "get_app_dir", lambda: app_dir)
        if existing_target:
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing writable user config")
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in {source, target, env, registry.path}
    }
    original_derivation = setup._minimax_config
    changed = source if edit == "source" else target
    peer_bytes = (json.dumps({**data, "device": {"peer_edit": "must survive"}}) + "\n").encode()

    def edit_during_derivation(parsed, name, model):
        assert parsed["device"] == data["device"]
        candidate = original_derivation(parsed, name, model)
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_bytes(peer_bytes)
        return candidate

    monkeypatch.setattr(setup, "_minimax_config", edit_during_derivation)
    with pytest.raises(ValueError, match="changed during setup"):
        setup.migrate_minimax(
            name="mini",
            api_key=KEY,
            base_url="https://mini.invalid/anthropic",
            model="MiniMax-M3.1-Flash",
        )
    for path, before in originals.items():
        assert (path.read_bytes() if path.exists() else None) == (
            peer_bytes if path == changed else before
        )
    assert not list(tmp_path.rglob("*.backup-*"))


@pytest.mark.parametrize("layout", ["selected", "wheel"])
def test_changes_present_before_migration_are_parsed_and_preserved_from_jsonc(
    config_files, registry, monkeypatch, tmp_path, layout
):
    from artemis.config import paths

    source, _, data = config_files
    data["device"]["before_setup"] = "keep existing revision"
    data["agent"]["memory"]["chunking"]["max_steps"] = 29
    source.write_text(
        "// Existing user revision, with JSONC comments\n" + json.dumps(data), encoding="utf-8"
    )
    source_before = source.read_bytes()
    if layout == "wheel":
        monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
        monkeypatch.delenv("ARTEMIS_ARTEMIS_JSON", raising=False)
        monkeypatch.setattr(paths, "get_main_config_path", lambda _: source)
        monkeypatch.setattr(paths, "is_source_checkout", lambda: False)
        monkeypatch.setattr(paths, "get_app_dir", lambda: tmp_path / "user-copy")
    target, _ = setup.migrate_minimax(
        name="mini",
        api_key=KEY,
        base_url="https://mini.invalid/anthropic",
        model="MiniMax-M3.1-Flash",
    )
    actual = json.loads(target.read_bytes())
    assert actual["device"] == data["device"]
    assert actual["agent"]["memory"]["chunking"]["max_steps"] == 29
    if layout == "wheel":
        assert source.read_bytes() == source_before and source != target


def test_wheel_target_edit_during_preflight_retains_peer_revision_without_publication(
    config_files, registry, monkeypatch, tmp_path
):
    from artemis.config import paths, task

    source, env, _ = config_files
    target = tmp_path / "user-copy" / "artemis.jsonc"
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSON", raising=False)
    monkeypatch.setattr(paths, "get_main_config_path", lambda _: source)
    monkeypatch.setattr(paths, "is_source_checkout", lambda: False)
    monkeypatch.setattr(paths, "get_app_dir", lambda: target.parent)
    source_before, env_before = source.read_bytes(), env.read_bytes()
    preflight = task.prepare_selected_task_config
    peer_bytes = b"later writable user config"

    def edit_after_baseline(*args, **kwargs):
        result = preflight(*args, **kwargs)
        target.write_bytes(peer_bytes)
        return result

    monkeypatch.setattr(task, "prepare_selected_task_config", edit_after_baseline)
    with pytest.raises(ValueError, match="changed during setup"):
        setup.migrate_minimax(
            name="mini",
            api_key=KEY,
            base_url="https://mini.invalid/anthropic",
            model="MiniMax-M3.1-Flash",
        )
    assert target.read_bytes() == peer_bytes
    assert source.read_bytes() == source_before and env.read_bytes() == env_before
    assert not registry.path.exists() and not list(tmp_path.rglob("*.backup-*"))


@pytest.mark.parametrize("replace_pointer", [False, True])
def test_unicode_selected_path_survives_dotenv_and_next_configuration_load(
    config_files, registry, monkeypatch, tmp_path, replace_pointer
):
    from dotenv import dotenv_values
    from artemis.config.llm import parse_llm_config
    from artemis.config.paths import get_main_config_path

    source, env, _ = config_files
    selected = tmp_path / "工作 空间" / "配置 文件.jsonc"
    selected.parent.mkdir()
    selected.write_bytes(source.read_bytes())
    if replace_pointer:
        env.write_text(
            env.read_text() + 'ARTEMIS_ARTEMIS_JSONC="old-selected.jsonc"\n', encoding="utf-8"
        )
    setup.migrate_minimax(
        name="mini",
        api_key=KEY,
        base_url="https://mini.invalid/anthropic",
        model="MiniMax-M3.1-Flash",
        config_path=selected,
    )
    next_environment = dotenv_values(stream=StringIO(env.read_text(encoding="utf-8")))
    assert next_environment["ARTEMIS_ARTEMIS_JSONC"] == str(selected.resolve())
    # Simulate a later process using dotenv's parsed pointer, not the in-process
    # os.environ value that migration set to the correct path directly.
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", next_environment["ARTEMIS_ARTEMIS_JSONC"])
    assert get_main_config_path("llm-config.json") == selected.resolve()
    loaded = parse_llm_config()
    assert loaded.operator.provider == "mini" and loaded.operator.model == "MiniMax-M3.1-Flash"
