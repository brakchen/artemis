"""D1 task ownership and auxiliary model regressions, entirely offline."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime
import inspect
import json
import sqlite3
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anthropic
import httpx
from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
import pytest
import pytest_asyncio

from apps.admin_console.services.model_service import ModelService
from artemis.config import settings
from artemis.config.agent import AgentGlobalConfig, MemoryChunkingConfig, StepSummarizerConfig
from artemis.config.llm import LLMConfig, _expand_default_into_nodes
from artemis.config.task import copy_task_config, get_task_agent_config
from artemis.context import ArtemisContext, DeviceContext
from artemis.llm import providers
from artemis.llm.providers import LLMProviderRegistry
from artemis.llm.reliability import CircuitBreaker, logical_call_scope
from artemis.llm.resolution import (
    context_uses_minimax,
    get_auxiliary_endpoint,
    get_effective_endpoint,
    public_model_snapshot,
)
from artemis.llm.router import ModelFactory
from artemis.memory.step_memory import StepLens, StepMemoryService
from artemis.sdk.agent import Agent
from artemis.sdk.types.agent import AgentConfig, ServerConfig
from artemis.services import llm as gateway
from artemis.services.token_meter import get_meter
from third_party.mobile_use.sdk.types.task import AgentProfile, Task, TaskRequest, TaskRequestCommon


def config(*, provider="mini-primary", model="default", fallback="mini-backup", nodes=None):
    return LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": provider,
                    "model": model,
                    "max_tokens": 12000,
                    "timeout_seconds": 47,
                    "reasoning_effort": "high",
                    "fallback": {
                        "provider": fallback,
                        "model": "default",
                        "max_tokens": 6000,
                        "timeout_seconds": 23,
                    },
                },
                "nodes": nodes or {},
            }
        )
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    result = LLMProviderRegistry(tmp_path / "providers.json")
    for name, model in (("mini-primary", "MiniMax-M3.1-Flash"), ("mini-backup", "MiniMax-M3")):
        result.add(
            name=name,
            kind="anthropic",
            vendor="minimax",
            model=model,
            api_key=f"fixture-{name}",
            api_base=f"https://{name}.invalid/anthropic",
        )
    monkeypatch.setattr(providers, "provider_registry", result)
    monkeypatch.setattr(ModelFactory, "_cache", {})
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", CircuitBreaker(threshold=1000))
    monkeypatch.setattr(gateway, "_get_current_data_engine", lambda: None)
    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "residual-fixture-google-key")
    monkeypatch.setattr(
        settings,
        "GOOGLE_API_KEY",
        Mock(get_secret_value=Mock(side_effect=AssertionError("residual Google credential read"))),
    )
    return result


def ctx(agent=None, llm=None):
    result = ArtemisContext(
        device=DeviceContext(device_id="offline-no-device"),
        llm_config=llm or config(),
        agent_config=agent,
    )
    result._genai_client = object()  # A residual shared Google client must be irrelevant.
    return result


def sdk_config(llm=None, **updates):
    profile = AgentProfile(name="selected-future-profile", llm_config=llm or config())
    fields = dict(
        agent_profiles={profile.name: profile},
        default_profile=profile,
        task_request_defaults=TaskRequestCommon(),
        servers=ServerConfig(adb_host="unused.invalid", adb_port=5037),
    )
    fields.update(updates)
    return AgentConfig(**fields)


class LockedHandler(BaseCallbackHandler):
    def __init__(self):
        self.lock = Lock()


@pytest.mark.parametrize("manager", [False, True])
def test_task_copy_preserves_explicitness_and_live_callbacks(registry, manager):
    handler = LockedHandler()
    callbacks = CallbackManager([handler]) if manager else [handler]
    original = sdk_config(graph_config_callbacks=callbacks)
    clone = copy_task_config(original)
    assert clone is not original
    assert clone.memory is not original.memory
    assert clone.default_profile.llm_config is not original.default_profile.llm_config
    assert clone.flash.step_summarizer.model_fields_set == set()
    assert clone.memory.chunking.model_fields_set == set()
    explicit = sdk_config(flash={"step_summarizer": {"model": "MiniMax-M3"}})
    assert copy_task_config(explicit).flash.step_summarizer.model_fields_set == {"model"}
    if manager:
        assert clone.graph_config_callbacks is callbacks
    else:
        assert clone.graph_config_callbacks is not callbacks
        assert clone.graph_config_callbacks[0] is handler
    original.memory.runtime.retry_limit = 99
    assert clone.memory.runtime.retry_limit != 99


@pytest.mark.asyncio
async def test_prewarm_never_constructs_a_client_before_profile_selection(registry, monkeypatch):
    from google import genai
    import langchain_google_genai
    import artemis.sdk.agent as module

    prohibited = Mock(side_effect=AssertionError("speculative Google client"))
    monkeypatch.setattr(genai, "Client", prohibited)
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", prohibited)
    progress = Mock()
    monkeypatch.setattr(module, "publish_startup_progress", progress)
    agent = Agent(config=sdk_config())
    await agent._prewarm_llm_connections("residual-explicit-google-key")
    prohibited.assert_not_called()
    progress.assert_called_once()
    assert progress.call_args.args[0] == "model_ready"


@pytest.mark.parametrize(
    "bad",
    [
        {"flash": {"step_summarizer": {"model": "gemini-explicit"}}},
        {"memory": {"chunking": {"model": "unknown-model"}}},
        {"flash": {"step_summarizer": {"thinking_budget": 4}}},
        {"memory": {"chunking": {"model": "MiniMax-M3", "reasoning_effort": "high"}}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_aux_fails_before_task_queue_session_and_probes(registry, monkeypatch, bad):
    import third_party.mobile_use.sdk.agent as vendored
    import artemis.sdk.agent as module

    agent = Agent(config=sdk_config(**bad))
    agent._initialized = True
    agent._device_context = DeviceContext(device_id="offline-no-device")
    agent._adb_client = agent._ui_adb_client = None
    task_factory = Mock(side_effect=AssertionError("Task was published"))
    data_engine = Mock(side_effect=AssertionError("session was started"))
    monkeypatch.setattr(vendored, "Task", task_factory)
    monkeypatch.setattr(module, "DataEngine", data_engine)
    agent._get_graph_state = Mock(side_effect=AssertionError("graph entered"))
    agent._prepare_device_environment = AsyncMock(side_effect=AssertionError("device probed"))
    with pytest.raises(ValueError, match="MiniMax|Thinking|effort"):
        await agent._run_task(
            TaskRequest(goal="offline", profile="selected-future-profile", record_trace=False)
        )
    assert agent._tasks == []
    task_factory.assert_not_called()
    data_engine.assert_not_called()
    agent._get_graph_state.assert_not_called()
    agent._prepare_device_environment.assert_not_called()
    paths = Mock(side_effect=AssertionError("trace paths created before preflight"))
    monkeypatch.setattr(agent, "_prepare_trace_paths", paths)
    with pytest.raises(ValueError, match="MiniMax|Thinking|effort"):
        agent._prepare_tracing(
            SimpleNamespace(request=TaskRequest(goal="offline", record_trace=False)), ctx()
        )
    paths.assert_not_called()
    data_engine.assert_not_called()
    invalid_context = ctx(AgentGlobalConfig.model_validate(bad))
    invalid_context.model_snapshot = {"roles": []}  # Public data cannot bypass preflight.
    with pytest.raises(ValueError, match="MiniMax|Thinking|effort"):
        agent._prepare_tracing(
            SimpleNamespace(request=TaskRequest(goal="offline", record_trace=False)),
            invalid_context,
        )
    paths.assert_not_called()
    data_engine.assert_not_called()


@pytest.mark.asyncio
async def test_actual_named_profile_preflight_rejects_mixed_vendor_before_publish(
    registry, monkeypatch
):
    from third_party.mobile_use.config.llm import LLM

    mixed = config()
    mixed.explorer = mixed.explorer.model_copy(
        update={"fallback": LLM(provider="ollama", model="local")}
    )
    selected = AgentProfile(name="future", llm_config=mixed)
    agent = Agent(config=sdk_config(agent_profiles={"future": selected}))
    agent._initialized = True
    agent._device_context = DeviceContext(device_id="offline-no-device")
    agent._adb_client = agent._ui_adb_client = None
    agent._get_graph_state = Mock(side_effect=AssertionError("published"))
    with pytest.raises(ValueError, match="mixed effective vendors"):
        await agent._run_task(TaskRequest(goal="offline", profile="future", record_trace=False))
    assert agent._tasks == []
    agent._get_graph_state.assert_not_called()


def test_task_context_uses_selected_profile_and_frozen_execution_setup(
    registry, monkeypatch, tmp_path
):
    import artemis.sdk.agent as module

    selected = sdk_config(
        graph_config_callbacks=[LockedHandler()],
        disable_checker=True,
        memory={"runtime": {"retry_limit": 7}},
    )
    agent = Agent(config=selected)
    context = ctx()
    request = TaskRequest(goal="offline", profile="pro", record_trace=False)
    agent._prepare_task_context(context, request, selected.default_profile)
    selected.disable_checker = False
    selected.memory.runtime.retry_limit = 100
    selected.default_profile.llm_config.operator.model = "mutated"
    assert context.agent_config.memory.runtime.retry_limit == 7
    assert context.agent_config.flash.step_summarizer.model_fields_set == set()
    assert get_effective_endpoint(context, "operator").model_name == "MiniMax-M3.1-Flash"
    assert context.model_snapshot["requested_profile"] == "pro"
    assert context.model_snapshot["selected_profile"] == "selected-future-profile"
    agent._tmp_traces_dir = tmp_path
    monkeypatch.setattr(agent, "_prepare_trace_paths", lambda task: "task-frozen")
    engine = Mock()
    monkeypatch.setattr(module, "DataEngine", lambda **kwargs: engine)
    task = Task(
        id=str(uuid4()),
        device=context.device,
        request=request,
        status="pending",
        created_at=datetime.now(),
    )
    agent._prepare_tracing(task, context)
    assert context.execution_setup.disable_checker is True
    assert context.execution_setup.enable_remote_tracing is False
    assert (
        engine.start_session.call_args.kwargs["device_info"]["model_snapshot"]
        == context.model_snapshot
    )
    public = json.dumps(context.model_snapshot)
    assert "fixture-mini" not in public and "api_key" not in public and "api_base" not in public
    assert "invalid/anthropic" not in public and "cache_key" not in public


def _offline_tracing_task(agent, context, monkeypatch, tmp_path, *, profile="pro"):
    import artemis.sdk.agent as module

    agent._tmp_traces_dir = tmp_path
    monkeypatch.setattr(agent, "_prepare_trace_paths", lambda task: "offline-tracing")
    engine = Mock()
    monkeypatch.setattr(module, "DataEngine", lambda **kwargs: engine)
    task = Task(
        id=str(uuid4()),
        device=context.device,
        status="pending",
        created_at=datetime.now(),
        request=TaskRequest(goal="offline", profile=profile, record_trace=False),
    )
    return task, engine


def _install_display_snapshot(context, kind, agent, profile="pro"):
    if kind == "valid":
        selected_llm = context.llm_config
        if selected_llm is None:
            selected_llm = agent._select_agent_profile(profile).llm_config
        snapshot = public_model_snapshot(
            SimpleNamespace(
                llm_config=selected_llm,
                agent_config=context.agent_config or agent._config,
            ),
            requested_profile=profile,
        )
    else:
        snapshot = {
            "none": None,
            "empty": {},
            "forged": {"roles": [{"role": "operator", "model": "forged-display"}]},
        }[kind]
    context.model_snapshot = snapshot


@pytest.mark.parametrize("public", ["none", "valid", "forged", "empty"])
@pytest.mark.parametrize("manager", [False, True])
def test_direct_tracing_freezes_config_independently_of_public_snapshot(
    registry,
    monkeypatch,
    tmp_path,
    public,
    manager,
):
    from artemis.tools.history import _memory_config

    handler = LockedHandler()
    callbacks = CallbackManager([handler]) if manager else [handler]
    original = sdk_config(
        graph_config_callbacks=callbacks,
        flash={"step_summarizer": {"retry_limit": 2}},
        memory={"runtime": {"retry_limit": 7}, "chunking": {"model": "MiniMax-M3"}},
    )
    agent = Agent(config=original)
    context = ctx(original, original.default_profile.llm_config)
    _install_display_snapshot(context, public, agent)
    task, _ = _offline_tracing_task(agent, context, monkeypatch, tmp_path)
    agent._prepare_tracing(task, context)
    assert context.agent_config is not original
    assert context.llm_config is not original.default_profile.llm_config
    assert context.agent_config.flash.step_summarizer.model_fields_set == {"retry_limit"}
    assert context.agent_config.memory.chunking.model_fields_set == {"model"}
    if manager:
        assert context.agent_config.graph_config_callbacks is callbacks
    else:
        assert context.agent_config.graph_config_callbacks is not callbacks
        assert context.agent_config.graph_config_callbacks[0] is handler
    original.memory.runtime.retry_limit = 99
    original.default_profile.llm_config.operator.model = "mutated-original"
    assert _memory_config(ctx=context).runtime.retry_limit == 7
    assert context.llm_config.operator.model == "default"
    assert get_effective_endpoint(context, "operator").model_name == "MiniMax-M3.1-Flash"


@pytest.mark.parametrize("public", ["none", "valid", "forged", "empty"])
def test_direct_tracing_missing_llm_uses_actual_named_profile(
    registry,
    monkeypatch,
    tmp_path,
    public,
):
    import artemis.config.llm as llm_module

    selected_llm = config()
    selected_llm.operator.model = "MiniMax-M3"
    selected_llm.operator.reasoning_effort = None
    named = AgentProfile(name="actual-named-llm", llm_config=selected_llm)
    original = sdk_config(agent_profiles={"future": named})
    agent = Agent(config=original)
    context = ArtemisContext(
        device=DeviceContext(device_id="offline-no-device"), agent_config=original
    )
    _install_display_snapshot(context, public, agent, "future")
    task, _ = _offline_tracing_task(agent, context, monkeypatch, tmp_path, profile="future")
    select = Mock(wraps=agent._select_agent_profile)
    monkeypatch.setattr(agent, "_select_agent_profile", select)
    global_config = Mock(side_effect=AssertionError("unrelated global LLM config loaded"))
    monkeypatch.setattr(llm_module, "get_default_llm_config", global_config)
    agent._prepare_tracing(task, context)
    select.assert_called_once_with("future")
    global_config.assert_not_called()
    assert context.selected_llm_profile == "actual-named-llm"
    assert context.llm_config is not selected_llm
    assert get_effective_endpoint(context, "operator").model_name == "MiniMax-M3"


@pytest.mark.parametrize("public", ["none", "valid", "forged", "empty"])
@pytest.mark.parametrize("has_agent_config", [False, True])
def test_direct_tracing_explicit_context_llm_has_priority(
    registry,
    monkeypatch,
    tmp_path,
    public,
    has_agent_config,
):
    import artemis.config.llm as llm_module

    original = sdk_config()
    agent = Agent(config=original)
    explicit = config()
    explicit.operator.model = "MiniMax-M3"
    explicit.operator.reasoning_effort = None
    context = ctx(original if has_agent_config else None, explicit)
    _install_display_snapshot(context, public, agent)
    task, _ = _offline_tracing_task(agent, context, monkeypatch, tmp_path)
    select = Mock(side_effect=AssertionError("explicit context LLM replaced by profile"))
    global_config = Mock(side_effect=AssertionError("explicit context LLM replaced by global"))
    monkeypatch.setattr(agent, "_select_agent_profile", select)
    monkeypatch.setattr(llm_module, "get_default_llm_config", global_config)
    agent._prepare_tracing(task, context)
    assert context.llm_config is not explicit
    assert context.llm_config.operator.model_fields_set == explicit.operator.model_fields_set
    explicit.operator.model = "mutated-original"
    assert context.llm_config.operator.model == "MiniMax-M3"
    assert get_effective_endpoint(context, "operator").model_name == "MiniMax-M3"
    select.assert_not_called()
    global_config.assert_not_called()


@pytest.mark.parametrize("public", ["none", "valid", "forged", "empty"])
def test_prepared_tracing_keeps_bound_config_and_endpoint_snapshot(
    registry,
    monkeypatch,
    tmp_path,
    public,
):
    import artemis.config as config_module
    import artemis.config.llm as llm_module

    original = sdk_config(
        memory={"runtime": {"retry_limit": 7}}, graph_config_callbacks=[LockedHandler()]
    )
    agent = Agent(config=original)
    context = ctx()
    task, _ = _offline_tracing_task(agent, context, monkeypatch, tmp_path)
    agent._prepare_task_context(context, task.request, original.default_profile)
    bound_config, bound_llm = context.agent_config, context.llm_config
    bound_endpoints = context._llm_endpoint_snapshot
    operator = get_effective_endpoint(context, "operator")
    _install_display_snapshot(context, public, agent)
    original.memory.runtime.retry_limit = 99
    original.default_profile.llm_config.operator.model = "mutated-original"
    registry.add(
        name="mini-primary",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3",
        api_key="changed-fixture-key",
        api_base="https://changed.invalid/anthropic",
    )
    deny = Mock(side_effect=AssertionError("prepared task reloaded configuration/registry"))
    monkeypatch.setattr(agent, "_select_agent_profile", deny)
    monkeypatch.setattr(config_module, "load_agent_config", deny)
    monkeypatch.setattr(llm_module, "get_default_llm_config", deny)
    monkeypatch.setattr(registry, "snapshot", deny)
    agent._prepare_tracing(task, context)
    assert context.agent_config is bound_config and context.llm_config is bound_llm
    assert context._llm_endpoint_snapshot is bound_endpoints
    assert context.agent_config.memory.runtime.retry_limit == 7
    assert get_effective_endpoint(context, "operator") == operator
    assert get_effective_endpoint(context, "operator").api_key == operator.api_key
    deny.assert_not_called()


@pytest.mark.parametrize("aux", ["step_summarizer", "memory_chunking"])
def test_aux_parameters_and_fallback_alias_are_task_pinned(registry, aux):
    context = ctx(AgentGlobalConfig())
    assert context_uses_minimax(context)
    primary = get_auxiliary_endpoint(context, aux)
    backup = get_auxiliary_endpoint(context, aux, use_fallback=True)
    assert primary.model_name == "MiniMax-M3.1-Flash"
    assert primary.reasoning_effort == "high" and primary.thinking_mode == "adaptive"
    assert primary.max_tokens == 12000 and primary.timeout_seconds == 47
    assert backup.model_name == "MiniMax-M3" and backup.reasoning_effort is None
    assert backup.api_key == "fixture-mini-backup" and backup.registry_name == "mini-backup"
    m3 = get_auxiliary_endpoint(context, aux, model_name="MiniMax-M3")
    assert m3.thinking_mode == "disabled" and m3.reasoning_effort is None
    assert m3.max_tokens == primary.max_tokens and m3.api_key == primary.api_key
    flash = get_auxiliary_endpoint(context, aux, model_name="MiniMax-M3.1-Flash", use_fallback=True)
    assert flash.thinking_mode == "adaptive" and flash.reasoning_effort == "medium"
    registry.add(
        name="mini-primary",
        kind="anthropic",
        vendor="minimax",
        api_key="new-fixture",
        api_base="https://changed.invalid/anthropic",
        model="MiniMax-M3",
    )
    context.agent_config.flash.step_summarizer.model = "gemini-later"
    assert get_auxiliary_endpoint(context, aux) == primary
    assert get_auxiliary_endpoint(context, aux).api_key == "fixture-mini-primary"


@pytest.mark.parametrize("model", ["MiniMax-M3", "MiniMax-M3.1-Flash"])
def test_explicit_aux_models_are_preserved_and_revalidated(registry, model):
    agent = AgentGlobalConfig(
        flash={"step_summarizer": {"model": model}}, memory={"chunking": {"model": model}}
    )
    context = ctx(agent)
    context_uses_minimax(context)
    for name in ("step_summarizer", "memory_chunking"):
        endpoint = get_auxiliary_endpoint(context, name)
        assert endpoint.model_name == model
        assert endpoint.reasoning_effort == (None if model == "MiniMax-M3" else "high")


def test_standalone_minimax_does_not_load_local_gemini_aux(registry, monkeypatch):
    import artemis.config as module
    from artemis.memory import ensure_step_memory
    from artemis.agents.operator.operator import OperatorNode

    loader = Mock(side_effect=AssertionError("unrelated local agent config was read"))
    monkeypatch.setattr(module, "load_agent_config", loader)
    context = ctx()
    summary = ensure_step_memory(context)
    assert summary._llm.endpoint.vendor == "minimax"
    assert get_task_agent_config(context) is context.agent_config
    operator = OperatorNode(context)
    context.agent_config.pro.execution.max_burst_actions = 2
    assert operator._max_burst_actions() == 2
    assert operator._transcript_cfg is context.agent_config.memory.transcript
    loader.assert_not_called()


@pytest.mark.parametrize("generic", [False, True])
def test_legacy_standalone_agent_config_loads_and_pins_once(monkeypatch, generic):
    import artemis.config as module
    from artemis.tools.history import _memory_config

    original = (
        SimpleNamespace(memory=SimpleNamespace(runtime=SimpleNamespace(retry_limit=6)))
        if generic
        else AgentGlobalConfig(memory={"runtime": {"retry_limit": 6}})
    )
    loader = Mock(return_value=original)
    monkeypatch.setattr(module, "load_agent_config", loader)
    context = SimpleNamespace(agent_config=None, llm_config=object())
    pinned = get_task_agent_config(context)
    assert pinned is not original and pinned.memory is not original.memory
    original.memory.runtime.retry_limit = 90
    assert get_task_agent_config(context) is pinned
    assert pinned.memory.runtime.retry_limit == 6
    assert _memory_config(ctx=context) is pinned.memory
    assert _memory_config(ctx=context).runtime.retry_limit == 6
    # An explicitly installed typed task configuration remains authoritative.
    replacement = AgentGlobalConfig(memory={"runtime": {"retry_limit": 2}})
    context.agent_config = replacement
    assert get_task_agent_config(context) is replacement
    loader.assert_called_once_with()


def test_pure_sdk_preflight_reuses_exact_profile_rules_without_device(registry):
    from third_party.mobile_use.sdk.types.exceptions import AgentProfileNotFoundError

    settings_config = sdk_config()
    alternate = AgentProfile(name="custom-llm", llm_config=config(model="MiniMax-M3.1-Flash"))
    settings_config.agent_profiles["future"] = alternate
    agent = Agent(config=settings_config)
    assert not agent._initialized
    assert agent.prepare_task_config("future").selected_llm_profile == "custom-llm"
    for architecture in (None, "flash", "pro", "ultra", "default", "PRO"):
        prepared = agent.prepare_task_config(architecture)
        assert prepared.selected_llm_profile == settings_config.default_profile.name
        assert prepared.model_snapshot["requested_profile"] == architecture
    with pytest.raises(AgentProfileNotFoundError):
        agent.prepare_task_config("missing")
    assert agent._tasks == []


def test_builder_carries_typed_memory_and_copies_each_result(registry, monkeypatch):
    import artemis.sdk.builders.agent_config_builder as module

    loaded = AgentGlobalConfig(memory={"runtime": {"retry_limit": 11}})
    monkeypatch.setattr(module, "load_agent_config", lambda: loaded)
    builder = module.AgentConfigBuilder()
    loaded.memory.runtime.retry_limit = 80
    first = builder.build(validate_profiles=False)
    assert first.memory.runtime.retry_limit == 11
    assert first.flash.step_summarizer.model_fields_set == set()
    first.memory.runtime.retry_limit = 70
    assert builder.build(validate_profiles=False).memory.runtime.retry_limit == 11


def test_flash_config_error_cannot_fall_back_to_google(registry, monkeypatch):
    import artemis.agents.flash.runner as module

    runner = object.__new__(module.FlashRunner)
    runner.ctx = ctx(AgentGlobalConfig(memory={"chunking": {"model": "gemini-explicit"}}))
    google = Mock(side_effect=AssertionError("Google fallback"))
    monkeypatch.setattr(module, "get_google_llm", google)
    with pytest.raises(ValueError, match="auxiliary model"):
        runner._init_llm()
    google.assert_not_called()


def sdk_body(text="fixture summary"):
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": "MiniMax-M3.1-Flash",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 8,
            "output_tokens": 5,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
        },
    }


@pytest_asyncio.fixture
async def sdk_transport(registry, monkeypatch):
    """Production MiniMax factory/SDK; only HTTP is replaced with MockTransport."""
    import google.genai
    import langchain_google_genai

    prohibited = Mock(side_effect=AssertionError("Google constructor or live HTTP prohibited"))
    monkeypatch.setattr(google.genai, "Client", prohibited)
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", prohibited)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", prohibited)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibited)
    requests = []
    responder = {"fn": lambda request, body: sdk_body()}

    async def handle(request):
        payload = (
            {"multipart": request.content}
            if request.headers.get("content-type", "").startswith("multipart/")
            else json.loads(request.content)
        )
        requests.append((request, payload))
        result = responder["fn"](request, payload)
        if inspect.isawaitable(result):
            result = await result
        if payload.get("stream") and isinstance(result, dict):
            # The module owners use the production streaming completion path.
            # Encode the same responder body as official Messages SSE events.
            start = {**result, "content": [], "stop_reason": None}
            events = [("message_start", {"message": start})]
            for index, block in enumerate(result["content"]):
                initial = (
                    {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
                )
                delta = (
                    {"type": "text_delta", "text": block["text"]}
                    if block["type"] == "text"
                    else {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                )
                events.extend(
                    [
                        ("content_block_start", {"index": index, "content_block": initial}),
                        ("content_block_delta", {"index": index, "delta": delta}),
                        ("content_block_stop", {"index": index}),
                    ]
                )
            events.extend(
                [
                    (
                        "message_delta",
                        {
                            "delta": {"stop_reason": result["stop_reason"], "stop_sequence": None},
                            "usage": result["usage"],
                        },
                    ),
                    ("message_stop", {}),
                ]
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(
                    f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
                    for name, data in events
                ),
            )
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    original = ModelFactory.create_model
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        responder["client"] = client

        def create(endpoint):
            assert endpoint.vendor == "minimax", "Google/other client construction is prohibited"
            model = original(endpoint)
            from artemis.llm.minimax_media import MiniMaxChatAnthropic

            assert isinstance(model, MiniMaxChatAnthropic)
            model._async_client = anthropic.AsyncAnthropic(
                api_key=endpoint.api_key,
                base_url=endpoint.api_base,
                max_retries=0,
                http_client=client,
            )
            return model

        monkeypatch.setattr(ModelFactory, "create_model", create)
        yield requests, responder


class RequestLens(StepLens):
    name = "d1_fixture"

    def __init__(self, context, *, invalid=False, fallback=False):
        self.llm = gateway.get_auxiliary_llm(context, "step_summarizer")
        self.backup = (
            gateway.get_auxiliary_llm(context, "step_summarizer", use_fallback=True)
            if fallback
            else None
        )
        self.invalid = invalid
        self.budgets = []

    async def render(self, key, payload):
        with logical_call_scope() as budget:
            self.budgets.append(budget)
            messages = [HumanMessage(content="offline source")]
            response = (
                await gateway.with_fallback(
                    lambda: self.llm.ainvoke(messages),
                    lambda: self.backup.ainvoke(messages),
                    none_should_fallback=False,
                )
                if self.backup
                else await self.llm.ainvoke(messages)
            )
            return None if self.invalid else str(response.content)


@pytest.mark.asyncio
async def test_pending_duplicate_uses_one_job_payload_and_scope(sdk_transport):
    requests, responder = sdk_transport
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request, payload):
        entered.set()
        await release.wait()
        return sdk_body()

    responder["fn"] = respond
    context = ctx(AgentGlobalConfig())
    lens = RequestLens(context)
    service = StepMemoryService(context, lens=lens)
    source = {"step_number": 1, "image": b"original"}
    service.submit("same", source)
    await asyncio.wait_for(entered.wait(), 5)
    task = service._pending_tasks["same"]
    service.submit("same", {"step_number": 99, "image": b"different"})
    assert service._pending_tasks["same"] is task
    assert service.get_job_payload("same") is source
    release.set()
    await service.flush()
    assert len(requests) == len(lens.budgets) == 1
    assert service.has_summary("same")


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_terminal_jobs_cannot_redispatch_or_reset_wire_budget(sdk_transport, permanent):
    requests, responder = sdk_transport
    if permanent:
        responder["fn"] = lambda *_: httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "offline invalid request"},
            },
        )
    context = ctx(AgentGlobalConfig())
    lens = RequestLens(context, invalid=True)
    service = StepMemoryService(context, lens=lens, retry_limit=30)
    service._retry_delays = (0.0,)
    payload = {"step_number": 1, "image": b"retained"}
    service.submit("terminal", payload)
    await service.flush()
    assert service.has_failed("terminal") and service.is_pending("terminal")
    assert len(requests) == (1 if permanent else 8)
    assert all(budget is lens.budgets[0] for budget in lens.budgets)
    service.submit("terminal", {"image": b"new"})
    await service.flush()
    assert len(requests) == (1 if permanent else 8)
    assert service.get_job_payload("terminal") is payload
    assert not service.has_summary("terminal")


@pytest.mark.asyncio
async def test_actual_aux_fallback_uses_its_alias_transport_and_key(sdk_transport):
    requests, responder = sdk_transport
    responder["fn"] = lambda request, _: (
        httpx.Response(
            503,
            headers={"retry-after": "0"},
            json={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "offline overloaded"},
            },
        )
        if request.url.host == "mini-primary.invalid"
        else sdk_body("fallback summary")
    )
    context = ctx(AgentGlobalConfig())
    lens = RequestLens(context, fallback=True)
    service = StepMemoryService(context, lens=lens)
    service.submit("fallback", {"step_number": 1})
    await service.flush()
    assert service.get_summary("fallback") == "fallback summary"
    assert len(requests) == 5
    assert [request.headers["x-api-key"] for request, _ in requests] == [
        "fixture-mini-primary"
    ] * 4 + ["fixture-mini-backup"]
    assert requests[0][1]["model"] == "MiniMax-M3.1-Flash"
    assert requests[0][1]["output_config"] == {"effort": "high"}
    assert requests[-1][1]["model"] == "MiniMax-M3"
    assert requests[-1][1]["thinking"] == {"type": "disabled"}
    assert "output_config" not in requests[-1][1]
    assert lens.budgets[0].attempts == 5


@pytest.mark.asyncio
async def test_aux_sdk_usage_once_preserves_ledger_base_and_wrapper_options(sdk_transport):
    from artemis.memory.chunking import HistoryChunkManager, StepCapsuleLens
    from artemis.memory.transcript import TranscriptLedger

    records = []
    engine = SimpleNamespace(
        current_session_id=str(uuid4()),
        current_step_id=None,
        record_trace=lambda **kwargs: records.append(kwargs),
    )
    context = ctx(AgentGlobalConfig())
    context.data_engine = engine
    wrapper = gateway.get_auxiliary_llm(context, "step_summarizer")
    wrapper.client_tools = frozenset({"allowed"})
    wrapper.allow_pause_turn = True
    configured = wrapper.with_config(tags=["offline"])
    for composed in (
        configured,
        wrapper | RunnableLambda(lambda value: value),
        wrapper.__ror__(RunnableLambda(lambda value: value)),
    ):
        assert composed.update_last_prompt is False
        assert composed.client_tools == frozenset({"allowed"}) and composed.allow_pause_turn
    structured = wrapper.with_structured_output(
        {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    )
    assert structured.with_config(tags=["offline"]).llm.update_last_prompt is False
    meter = get_meter(engine.current_session_id)
    meter.last_prompt_tokens = 777
    await configured.ainvoke([HumanMessage(content="offline")])
    usage = [row for row in records if row.get("name") == "llm_usage"]
    assert len(usage) == 1
    assert usage[0]["payload"]["total_tokens"] == 19
    assert meter.last_prompt_tokens == 777
    ledger = TranscriptLedger()
    ledger.record_prompt_tokens(12345)
    chunker = HistoryChunkManager(ctx=context, capsule_service=StepMemoryService(context))
    assert chunker._context_base_tokens(ledger) == 12345
    # The legacy injected wrapper path is metered once with the same semantics.
    lens = StepCapsuleLens(ctx=context, llm=wrapper)
    await lens._invoke(wrapper, [HumanMessage(content="offline")], "injected")
    assert len([row for row in records if row.get("name") == "llm_usage"]) == 2
    assert meter.last_prompt_tokens == 777


def test_current_display_canonical_and_history_does_not_follow_config(registry, monkeypatch):
    import artemis.config as module

    context = ctx(AgentGlobalConfig())
    snapshot = public_model_snapshot(context, requested_profile="pro")
    row = {"device_info": json.dumps({"model_snapshot": snapshot})}
    monkeypatch.setattr(module, "parse_llm_config", config)
    monkeypatch.setattr(module, "load_agent_config", AgentGlobalConfig)
    monkeypatch.setattr(
        ModelFactory,
        "create_model",
        Mock(side_effect=AssertionError("display constructed a client")),
    )
    assert ModelService.get_active_model_info("pro")["provider"] == "anthropic"
    registry.add(
        name="mini-primary",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3",
        api_key="changed",
        api_base="https://changed.invalid/anthropic",
    )
    # A fresh current view diagnoses unsupported inherited effort instead of caching an old value.
    assert ModelService.get_active_model_info("pro")["id"] == "unknown"
    historical = ModelService.get_session_model_info(row, "pro")
    assert historical["id"] == "MiniMax-M3.1-Flash" and historical["provider"] == "anthropic"
    assert ModelService.get_session_model_info({"device_info": "{}"}, "flash")["id"] == "unknown"


@pytest.mark.parametrize(
    "env_name", ["ARTEMIS_ARTEMIS_JSONC", "ARTEMIS_ARTEMIS_JSON", "ARTEMIS_LLM_CONFIG_JSON"]
)
def test_selected_main_path_is_shared_and_explicit_missing_is_not_swallowed(
    tmp_path, monkeypatch, env_name
):
    from artemis.config import paths
    from artemis.config.agent import load_agent_config
    from artemis.config.llm import parse_llm_config

    for key in (
        "ARTEMIS_ARTEMIS_JSONC",
        "ARTEMIS_ARTEMIS_JSON",
        "ARTEMIS_LLM_CONFIG_JSON",
        "ARTEMIS_AGENT_CONFIG_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    selected = tmp_path / "selected.json"
    selected.write_text(
        json.dumps(
            {
                "default": {
                    "provider": "unit-test-llm",
                    "model": "selected-model",
                    "fallback": {"provider": "unit-test-llm", "model": "selected-backup"},
                },
                "agent": {"memory": {"runtime": {"retry_limit": 9}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(env_name, str(selected))
    assert parse_llm_config().operator.model == "selected-model"
    if env_name != "ARTEMIS_LLM_CONFIG_JSON":
        assert load_agent_config().memory.runtime.retry_limit == 9
    missing = tmp_path / "missing.json"
    monkeypatch.setenv(env_name, str(missing))
    with pytest.raises(FileNotFoundError, match="Explicit"):
        paths.get_main_config_path("llm-config.json")
    if env_name != "ARTEMIS_LLM_CONFIG_JSON":
        with pytest.raises(FileNotFoundError, match="Explicit"):
            load_agent_config()


def test_explicit_legacy_agent_and_llm_configs_remain_independent(tmp_path, monkeypatch):
    from artemis.config.agent import load_agent_config
    from artemis.config.llm import parse_llm_config

    for name in ("ARTEMIS_ARTEMIS_JSONC", "ARTEMIS_ARTEMIS_JSON"):
        monkeypatch.delenv(name, raising=False)
    agent_path, llm_path = tmp_path / "agent.json", tmp_path / "llm.json"
    agent_path.write_text('{"memory":{"runtime":{"retry_limit":8}}}', encoding="utf-8")
    llm_path.write_text(
        json.dumps(
            {
                "default": {
                    "provider": "unit-test-llm",
                    "model": "legacy-primary",
                    "fallback": {"provider": "unit-test-llm", "model": "legacy-backup"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARTEMIS_AGENT_CONFIG_JSON", str(agent_path))
    monkeypatch.setenv("ARTEMIS_LLM_CONFIG_JSON", str(llm_path))
    agent, llm = load_agent_config(), parse_llm_config()
    assert agent.memory.runtime.retry_limit == 8 and llm.operator.model == "legacy-primary"
    assert agent.flash.step_summarizer.model_fields_set == set()
    monkeypatch.setenv("ARTEMIS_AGENT_CONFIG_JSON", str(tmp_path / "missing-agent"))
    with pytest.raises(FileNotFoundError):
        load_agent_config()
    assert parse_llm_config().operator.model == "legacy-primary"


@pytest.mark.asyncio
async def test_actual_visual_and_capsule_lenses_use_pinned_aux_models(sdk_transport):
    from artemis.agents.flash.summarizer import VisualStepSummarizer
    from artemis.memory.chunking import StepCapsuleLens

    requests, responder = sdk_transport
    context = ctx(AgentGlobalConfig())
    visual = VisualStepSummarizer(context, model_name="MiniMax-M3")
    visual.dispatch(1, "wait", {}, None, None, "observed", action_key="visual")
    await visual.flush()
    assert visual.has_summary("visual")
    assert requests[0][1]["model"] == "MiniMax-M3"
    assert "output_config" not in requests[0][1]
    capsule = {
        "doing": "Inspecting a screen",
        "did": "Observed a screen",
        "effect": "A label was visible",
        "entry_state": "Screen",
        "exit_state": "Screen",
        "verified_facts": [],
        "unresolved": [],
        "failed_paths": [],
        "important_entities": [],
        "intervals": [{"start_step": 1, "end_step": 1, "text": "Observed the screen"}],
    }
    responder["fn"] = lambda *_: sdk_body(json.dumps(capsule))
    lens = StepCapsuleLens(ctx=context, model_name="MiniMax-M3.1-Flash")
    rendered = await lens.render(
        "capsule", {"start_step": 1, "end_step": 1, "steps": [{"step_number": 1}]}
    )
    assert json.loads(rendered)["did"] == capsule["did"]
    assert requests[-1][1]["output_config"] == {"effort": "high"}
    for constructor in (VisualStepSummarizer, StepCapsuleLens):
        with pytest.raises(ValueError, match="auxiliary model"):
            constructor(ctx=context, model_name="gemini-explicit")


@pytest.mark.asyncio
async def test_raw_gemini_roles_and_residual_shared_client_use_actual_minimax(sdk_transport):
    requests, _ = sdk_transport
    context = ctx(
        AgentGlobalConfig(),
        config(provider="google", model="gemini-raw-residue", fallback="google"),
    )
    # Registry default precedence, rather than raw provider/model names or a
    # residual Google credential/client, decides the auxiliary transport.
    wrapper = gateway.get_auxiliary_llm(context, "step_summarizer")
    await wrapper.ainvoke([HumanMessage(content="offline")])
    assert wrapper.endpoint.vendor == "minimax"
    assert requests[0][0].url.host == "mini-primary.invalid"
    assert requests[0][1]["model"] == "MiniMax-M3.1-Flash"


def test_replay_invalid_current_config_is_not_swallowed_or_sandboxed(
    registry, monkeypatch, tmp_path
):
    from apps.admin_console.replay_manager import ReplayManager
    import artemis.config as module

    manager = ReplayManager(workspace_root=tmp_path, original_db_path=tmp_path / "unused.db")
    prepared = Mock(side_effect=AssertionError("sandbox created before preflight"))
    monkeypatch.setattr(manager, "_ensure_session_chunked", prepared)
    monkeypatch.setattr(module, "get_default_llm_config", config)
    monkeypatch.setattr(
        module,
        "load_agent_config",
        lambda: AgentGlobalConfig(memory={"chunking": {"model": "gemini-explicit"}}),
    )
    with pytest.raises(ValueError, match="auxiliary model"):
        manager.create_ctx(str(uuid4()), 1)
    prepared.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["ask_explorer", "ask_image_processor"])
async def test_minimax_replay_context_and_actual_sdk_path_never_import_google_models(
    sdk_transport, monkeypatch, tmp_path, tool_name
):
    from apps.admin_console import replay_manager as module
    from artemis.graph.state import State
    import artemis.config as config_module
    import artemis.data_engine.engine as engine_module
    import artemis.tools.explorer_tool as explorer_module

    sid = str(uuid4())
    manager = module.ReplayManager(workspace_root=tmp_path, original_db_path=tmp_path / "master.db")
    manager.replay_base_dir = tmp_path / "replay"
    manager.test_data_dir = tmp_path / "data"
    manager.test_outputs_dir = tmp_path / "outputs"
    step_dir = manager.test_data_dir / f"{sid}_chunked" / "step_01"
    step_dir.mkdir(parents=True)
    step = {"step_id": str(uuid4()), "session_id": sid, "step_number": 1}
    (step_dir / "step.json").write_text(json.dumps(step), encoding="utf-8")
    historical_snapshot = {
        "roles": [
            {"role": "operator", "fallback": False, "kind": "google", "model": "historical-gemini"}
        ]
    }
    with sqlite3.connect(manager.db_path) as conn:
        conn.execute("CREATE TABLE sessions(session_id TEXT, device_info TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            (sid, json.dumps({"model_snapshot": historical_snapshot})),
        )
    monkeypatch.setattr(manager, "_ensure_session_chunked", lambda *_: None)
    monkeypatch.setattr(manager, "extract_replay_data", lambda *args, **kwargs: ([], [], []))
    monkeypatch.setattr(manager, "load_session_goal", lambda *args: "offline replay")
    monkeypatch.setattr(manager, "setup_temporary_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_preemptive_clip_video", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        manager, "instantiate_state", lambda **kwargs: State(initial_goal="offline replay")
    )
    monkeypatch.setattr(config_module, "get_default_llm_config", config)
    monkeypatch.setattr(config_module, "load_agent_config", AgentGlobalConfig)
    records = []

    class Engine:
        def __init__(self, context):
            self.ctx = context
            self.current_step_id = None
            self.current_session_id = None

        def record_trace(self, **kwargs):
            records.append(kwargs)

        async def shutdown(self):
            pass

    monkeypatch.setattr(engine_module, "DataEngine", Engine)
    monkeypatch.setattr(settings, "DATA_ENGINE_DB_PATH", tmp_path / "restore.db")
    monkeypatch.setattr(settings, "TRACES_PATH", tmp_path / "restore-traces")
    monkeypatch.setenv("DATA_ENGINE_DB_PATH", str(tmp_path / "restore.db"))
    contexts = []

    async def replay_tool(ctx, state, instruction="offline"):
        contexts.append(ctx)
        response = await gateway.get_llm(ctx, "explorer").ainvoke(
            [HumanMessage(content=instruction)]
        )
        return str(response.content)

    monkeypatch.setattr(explorer_module, "_run_explorer_logic", replay_tool)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "google.genai.models":
            raise AssertionError("MiniMax replay imported AsyncModels")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    if tool_name == "ask_image_processor":
        from langchain_core.tools import ToolException

        with pytest.raises(ToolException, match="unsupported for MiniMax"):
            await manager.replay_step_tool(sid, 1, replay_id=str(uuid4()), tool_name=tool_name)
        assert not sdk_transport[0] and not contexts
        results = list(manager.test_outputs_dir.rglob("result.json"))
        assert len(results) == 1
        rejected = json.loads(results[0].read_text(encoding="utf-8"))
        assert rejected["success"] is False and "unsupported" in rejected["error"]
        return
    await manager.replay_step_tool(sid, 1, replay_id=str(uuid4()), tool_name=tool_name)
    assert contexts and isinstance(contexts[0].agent_config, AgentGlobalConfig)
    assert get_effective_endpoint(contexts[0], "explorer").vendor == "minimax"
    assert any(row.get("name") == "llm_usage" for row in records)
    assert any(row.get("status") == "running" for row in records)
    original_row = {"device_info": json.dumps({"model_snapshot": historical_snapshot})}
    assert ModelService.get_session_model_info(original_row)["id"] == "historical-gemini"
    assert (
        next(
            row
            for row in contexts[0].model_snapshot["roles"]
            if row["role"] == "operator" and not row["fallback"]
        )["model"]
        == "MiniMax-M3.1-Flash"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["main", "operator", "planner", "explorer", "detector", "ocr", "search", "video"]
)
async def test_actual_module_sdk_matrix_with_residual_google_and_live_http_denied(
    sdk_transport, monkeypatch, tmp_path, surface
):
    """Exercise module owners with production resolution, client factory and SDK."""
    import base64
    from io import BytesIO

    from PIL import Image
    from artemis.graph.state import State
    from artemis.utils.vision import create_snapshot

    requests, responder = sdk_transport
    selected = config(provider="google", model="gemini-raw-residue", fallback="google")
    selected.default.enable_grounding = True
    context = ctx(AgentGlobalConfig(), selected)
    image = BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, "JPEG")
    data = image.getvalue()
    screenshot = tmp_path / "synthetic.jpg"
    screenshot.write_bytes(data)
    snapshot = create_snapshot(context, data)
    state = State(initial_goal="offline request")
    state.latest_screenshot = str(screenshot)
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {
        "screenshot_b64": base64.b64encode(data).decode(),
        "xml_hierarchy": [],
        "snapshot": snapshot.model_dump(),
        "width": 100,
        "height": 100,
    }
    monkeypatch.setattr(settings, "TRACES_PATH", tmp_path / "traces")
    monkeypatch.setattr(settings, "DATA_ENGINE_DB_PATH", tmp_path / "unused.db")

    def text_reply(value):
        return sdk_body(json.dumps(value))

    def tool_reply(name, args):
        result = sdk_body()
        result.update(
            content=[{"type": "tool_use", "id": "matrix", "name": name, "input": args}],
            stop_reason="tool_use",
        )
        return result

    if surface == "main":
        from artemis.agents.flash.runner import FlashRunner

        runner = object.__new__(FlashRunner)
        runner.ctx = context
        response = await runner._init_llm().ainvoke([HumanMessage(content="offline request")])
        assert response.content == "fixture summary"
    elif surface == "operator":
        from artemis.agents.operator.operator import OperatorNode
        from langchain_core.messages import SystemMessage
        from langchain_core.tools import StructuredTool

        async def lookup() -> str:
            """Return an offline fixture fact."""
            return "fixture fact"

        monkeypatch.setattr("artemis.agents.operator.operator.OPERATOR_MAX_TOOL_ITERATIONS", 2)
        responder["fn"] = lambda *_: tool_reply("lookup", {}) if len(requests) == 1 else sdk_body()
        node = OperatorNode(context, tools=[StructuredTool.from_function(coroutine=lookup)])
        result = await node(state)
        assert result["structured_decisions"] is None
        await node(state)
        assert len(requests) == 3
        assert "maximum number of tool calls" in str(requests[1][1]["messages"][-1])
        assert context.transcript_ledger.turn_count == 1
        active = context.transcript_ledger.active_messages
        assert active and isinstance(active[0], HumanMessage)
        assert not any(isinstance(message, SystemMessage) for message in active)
    elif surface == "planner":
        from artemis.agents.planner.planner import run_async_planner_validation

        responder["fn"] = lambda *_: text_reply({"is_approved": True, "feedback": "fixture"})
        result = await run_async_planner_validation(context, "offline request", "before", "after")
        assert result == {"status": "success", "feedback": "fixture"}
    elif surface == "explorer":
        from artemis.agents.explorer.explorer import Explorer

        responder["fn"] = lambda *_: tool_reply(
            "submit_answer", {"candidates": [], "fallback_message": "fixture unknown"}
        )
        result = await Explorer(context).run(
            "nontext icon", "", str(screenshot), state, version="ultra"
        )
        assert json.loads(result) == {"candidates": [], "fallback_message": "fixture unknown"}
        assert all(tool["name"] != "ask_image_processor" for tool in requests[0][1]["tools"])
        assert "ask_image_processor" not in str(requests[0][1]["system"])
    elif surface == "detector":
        from artemis.agents.object_detector.object_detector import _run_object_detection

        responder["fn"] = lambda *_: text_reply(
            {"status": "found", "candidates": [{"label": "fixture", "bbox": [100, 200, 500, 600]}]}
        )
        result = await _run_object_detection(context, data, ["fixture"], snapshot=snapshot)
        assert result["detected"][0]["point"] == [300, 400]
        assert result["detected"][0]["snapshot_id"] == snapshot.capture_id
    elif surface == "ocr":
        from artemis.utils.ocr_api import perform_ocr

        responder["fn"] = lambda *_: text_reply(
            {
                "status": "complete",
                "regions": [{"text": "fixture", "bbox": [100, 200, 500, 600], "uncertain": False}],
            }
        )
        result = await perform_ocr(base64.b64encode(data).decode(), ctx=context, snapshot=snapshot)
        assert result[0]["text"] == "fixture" and result[0]["source"] == "minimax"
    elif surface == "search":
        from artemis.services.web_search import web_search

        def search_reply(*_):
            result = sdk_body("fixture source")
            result["content"].append(
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "search",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://source.invalid/fixture",
                            "title": "fixture",
                            "encrypted_content": "opaque",
                        }
                    ],
                }
            )
            return result

        responder["fn"] = search_reply
        result = await web_search(context, "offline fact")
        assert (
            result.status == "complete"
            and result.sources[0].url == "https://source.invalid/fixture"
        )
    else:
        from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer

        responder["fn"] = lambda *_: tool_reply(
            "submit_answer",
            {
                "summary": "fixture",
                "analysis": "synthetic frame",
                "timeline_events": [],
                "temporal_evidence_sufficient": True,
                "unknowns": [],
            },
        )
        monkeypatch.setattr(
            "artemis.agents.video_analyzer.video_analyzer.probe_duration",
            AsyncMock(return_value=1.0),
        )
        monkeypatch.setattr(
            "artemis.agents.video_analyzer.video_analyzer.extract_frames_at_timestamps",
            lambda path, timestamps, **kwargs: [(0.0, data), (0.5, data)],
        )
        analyzer = VideoAnalyzer(context)
        media = tmp_path / "synthetic.mp4"
        media.write_bytes(b"synthetic fixture; decoding is supplied by the frame fixture")
        result = await analyzer._exec_single_chunk_universal(
            media, media, 0.0, 1.0, 0.0, "offline", "fixture"
        )
        assert "fixture" in result and analyzer.media_coverage[0]["strategy"] == "keyframes"

    assert requests
    for request, payload in requests:
        assert request.url.host == "mini-primary.invalid"
        assert payload["model"] == "MiniMax-M3.1-Flash"
        assert request.headers["x-api-key"] == "fixture-mini-primary"


@pytest.mark.asyncio
async def test_native_asr_uses_same_offline_transport_and_task_key(sdk_transport, tmp_path):
    from artemis.agents.video_analyzer.native_speech import transcribe_audio

    requests, responder = sdk_transport
    responder["fn"] = lambda *_: httpx.Response(
        200, json={"segments": [{"start": 0.0, "end": 1.0, "text": "fixture speech"}]}
    )
    selected = get_effective_endpoint(ctx(AgentGlobalConfig()), "video_analyzer", is_utils=True)
    selected = selected.model_copy(update={"api_base": "https://api.minimax.cn/anthropic"})
    audio = tmp_path / "synthetic.mp3"
    audio.write_bytes(b"synthetic audio; duration supplied by the fixture")
    result = await transcribe_audio(
        selected,
        audio,
        actual_start=5.0,
        duration=1.0,
        work_dir=tmp_path,
        client=responder["client"],
    )
    assert result.status == "transcribed" and result.segments[0]["start"] == 5.0
    request, payload = requests[0]
    assert request.url.path == "/v1/speech_to_text"
    assert request.headers["authorization"] == "Bearer fixture-mini-primary"
    assert b'filename="synthetic.mp3"' in payload["multipart"]


@pytest.mark.asyncio
async def test_operator_final_iteration_reminder_keeps_actual_sdk_message_order(
    sdk_transport, monkeypatch
):
    from langchain_core.messages import SystemMessage
    from langchain_core.tools import StructuredTool
    from artemis.agents.operator.operator import OperatorNode
    from artemis.graph.state import State

    requests, responder = sdk_transport
    context = ctx(AgentGlobalConfig())
    monkeypatch.setattr("artemis.agents.operator.operator.OPERATOR_MAX_TOOL_ITERATIONS", 2)

    async def lookup() -> str:
        """Return an offline fixture fact."""
        return "fixture fact"

    def respond(*_):
        result = sdk_body("fixture final thought")
        if len(requests) == 1:
            result.update(
                content=[{"type": "tool_use", "id": "lookup", "name": "lookup", "input": {}}],
                stop_reason="tool_use",
            )
        return result

    responder["fn"] = respond
    messages = [SystemMessage(content="fixture policy"), HumanMessage(content="offline")]
    result = await OperatorNode(context)._invoke_llm_loop(
        gateway.get_llm(context, "operator"),
        messages,
        [StructuredTool.from_function(coroutine=lookup)],
        [],
        State(initial_goal="offline"),
    )
    assert result[1] == "fixture final thought" and len(requests) == 2
    assert "MiniMax execution contract" in str(requests[0][1]["system"])
    assert "maximum number of tool calls" in str(requests[1][1]["messages"][-1])


@pytest.mark.asyncio
async def test_image_coder_invalid_real_config_propagates_before_model_or_kernel(
    registry, monkeypatch
):
    from artemis.agents.image_processor import image_processor as module

    context = ctx(AgentGlobalConfig(memory={"chunking": {"model": "gemini-explicit"}}))
    prohibited = Mock(side_effect=AssertionError("invalid task constructed model or kernel"))
    monkeypatch.setattr(module, "get_llm", prohibited)
    monkeypatch.setattr(module, "PythonExecutor", prohibited)
    with pytest.raises(ValueError, match="auxiliary model"):
        await module.ImageProcessor(context).run("opaque", "missing.png")
    prohibited.assert_not_called()
