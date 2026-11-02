"""Regression coverage for config-to-wire MiniMax endpoint foundations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from artemis.config.llm import (
    AGENT_ROLES,
    LLMConfig,
    _expand_default_into_nodes,
    deep_merge_llm_config,
    load_llm_config_override,
    parse_llm_config,
)
from artemis.llm import providers
from artemis.llm.providers import LLMProviderRegistry, discover_models, resolve_model_config
from artemis.llm.resolution import (
    context_uses_minimax,
    get_effective_default_endpoint,
    get_effective_endpoint,
)
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from third_party.mobile_use.config.llm import LLM


def _register(registry, **kwargs):
    return registry.add(
        name="minimax-prod",
        kind="anthropic",
        vendor="minimax",
        api_base="https://mini.invalid/anthropic",
        api_key="test-minimax-key",
        model="MiniMax-M3.1-Flash",
        **kwargs,
    )


def _config(default=None, nodes=None):
    default = default or {
        "provider": "minimax-prod",
        "model": "default",
        "fallback": {"provider": "minimax-prod", "model": "default"},
    }
    return LLMConfig.model_validate(
        _expand_default_into_nodes({"default": default, "nodes": nodes or {}})
    )


@pytest.fixture
def mini_registry(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    _register(registry)
    monkeypatch.setattr(providers, "provider_registry", registry)
    return registry


def test_custom_alias_vendor_and_secret_views(mini_registry):
    config = _config()
    endpoint = get_effective_endpoint(SimpleNamespace(llm_config=config), "planner")
    resolved = resolve_model_config("minimax-prod", "default")
    assert endpoint.provider == "anthropic"
    assert endpoint.vendor == resolved.vendor == "minimax"
    assert endpoint.registry_name == resolved.registry_name == "minimax-prod"
    assert endpoint.max_tokens == 8192
    assert endpoint.reasoning_effort == "medium"
    assert "test-minimax-key" not in repr(endpoint)
    assert "api_key" not in endpoint.model_dump()
    assert "api_key" not in resolved.model_dump()
    assert "test-minimax-key" not in str(mini_registry.get("minimax-prod").to_public_dict())


def test_vendor_is_explicit_not_a_name_or_model_heuristic(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    registry.add(
        name="minimax-looking",
        kind="anthropic",
        api_key="k",
        api_base="https://mini.invalid",
        model="MiniMax-M3.1-Flash",
    )
    monkeypatch.setattr(providers, "provider_registry", registry)
    assert resolve_model_config("minimax-looking", "default").vendor is None
    with pytest.raises(ValidationError, match="requires kind=anthropic"):
        registry.add(name="wrong", kind="openai", vendor="minimax", api_key="k")


@pytest.mark.parametrize("provider", ["unknown-alias", "minimax-prod", "default"])
def test_unknown_and_disabled_explicit_resolution_fail(mini_registry, provider):
    mini_registry.add(name="minimax-prod", api_key=None, enabled=False)
    with pytest.raises(ValueError):
        resolve_model_config(provider, "default")


def test_registered_missing_model_cannot_inherit_gemini(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    registry.add(name="missing-model", api_key="k")
    monkeypatch.setattr(providers, "provider_registry", registry)
    with pytest.raises(ValueError, match="no default model"):
        resolve_model_config("google", "gemini-existing")


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
def test_strict_positive_output_budget(value):
    with pytest.raises(ValidationError):
        LLM(provider="minimax-prod", model="default", max_tokens=value)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_finite_positive_timeout(value):
    with pytest.raises(ValidationError):
        LLM(provider="minimax-prod", model="default", timeout_seconds=value)


def test_timeout_alias_conflict_and_profile_alias_merge():
    with pytest.raises(ValidationError, match="conflict"):
        LLM(provider="openai", model="m", timeout=2, timeout_seconds=3)
    config = deep_merge_llm_config(_config(), {"nodes": {"operator": {"timeout": 44}}})
    assert config.operator.timeout_seconds == config.operator.timeout == 44


def test_normalization_includes_optional_roles_and_preserves_separate_fallback():
    default = {
        "provider": "minimax-prod",
        "model": "default",
        "max_tokens": 10000,
        "timeout_seconds": 70,
        "fallback": {
            "provider": "minimax-prod",
            "model": "default",
            "max_tokens": 5000,
            "timeout_seconds": 22,
        },
    }
    config = _config(
        default,
        {
            "output_analyzer": {"max_tokens": 12000},
            "history_analyzer": {"fallback": {"max_tokens": 3500}},
        },
    )
    for role in AGENT_ROLES:
        if getattr(config, role) is not None:
            assert config.get_agent(role).provider == "minimax-prod"
    assert config.output_analyzer.max_tokens == 12000
    assert config.history_analyzer.fallback.max_tokens == 3500
    assert config.history_analyzer.fallback.timeout_seconds == 22
    assert config.utils.video_analyzer.max_tokens == 10000
    assert config.utils.object_detector.fallback.max_tokens == 5000


def test_profiles_apply_only_explicit_fields_without_gemini_injection():
    config = _config()
    merged = deep_merge_llm_config(config, {"nodes": {"operator": {"max_tokens": 9999}}})
    assert merged.planner == config.planner
    assert merged.operator.provider == "minimax-prod"
    assert merged.operator.max_tokens == 9999
    assert merged.operator.fallback == config.operator.fallback
    assert config.operator.max_tokens is None
    merged = deep_merge_llm_config(
        merged, {"default": {"timeout_seconds": 88, "fallback": {"max_tokens": 2000}}}
    )
    assert merged.planner.timeout_seconds == 88
    assert merged.operator.max_tokens == 9999
    assert merged.utils.hopper.fallback.max_tokens == 2000


def test_legacy_flat_profile_partial_default_materializes_operator_baseline():
    configured = _config(
        {
            "provider": "openai",
            "model": "global-main",
            "fallback": {"provider": "openai", "model": "global-backup"},
        },
        {
            "operator": {"model": "operator-main"},
            "planner": {"model": "planner-main", "fallback": {"model": "planner-backup"}},
        },
    )
    flattened = LLMConfig.model_validate(configured.model_dump(exclude={"default"}))
    assert flattened.default is None
    merged = deep_merge_llm_config(flattened, {"default": {"timeout_seconds": 88}})
    assert merged.default.provider == "openai"
    assert merged.default.model == "operator-main"
    assert merged.default.fallback == flattened.operator.fallback
    assert merged.default.timeout_seconds == 88
    assert merged.operator.model == "operator-main"
    assert merged.planner.model == "planner-main"
    assert merged.planner.fallback.model == "planner-backup"
    assert merged.operator.timeout_seconds == merged.planner.timeout_seconds == 88
    assert merged.utils.hopper.model == flattened.utils.hopper.model
    assert merged.utils.hopper.timeout_seconds == 88
    assert flattened.default is None


@pytest.mark.parametrize(
    "data",
    [
        {"nodes": {"planer": {}}},
        {"nodes": {"ocr": {}}},
        {"nodes": {"operator": {"max_token": 3}}},
        {"nodes": {"operator": {"fallback": {"oops": 1}}}},
        {"utils": {"unknown_util": {}}},
    ],
)
def test_unknown_roles_and_parameters_rejected(data):
    with pytest.raises((ValueError, ValidationError)):
        deep_merge_llm_config(_config(), data)


def test_unrelated_main_config_sections_remain_valid():
    data = {
        "default": _config().default.model_dump(),
        "agent": {"anything": True},
        "device": {"serial": "unchanged"},
    }
    config = LLMConfig.model_validate(_expand_default_into_nodes(data))
    assert config.planner.provider == "minimax-prod"
    assert data["device"] == {"serial": "unchanged"}


@pytest.mark.parametrize(
    "model_type, data",
    [
        (LLM, {"provider": "openai", "model": "m", "api_key": "sentinel-secret-never-in-errors"}),
        (
            providers.LLMProvider,
            {
                "name": "entry",
                "kind": "openai",
                "vendor": "minimax",
                "api_key": "sentinel-secret-never-in-errors",
            },
        ),
        (
            ModelEndpoint,
            {
                "provider": "anthropic",
                "vendor": "minimax",
                "api_key": "sentinel-secret-never-in-errors",
            },
        ),
    ],
)
def test_validation_error_strings_do_not_echo_secret_inputs(model_type, data):
    with pytest.raises(ValidationError) as caught:
        model_type.model_validate(data)
    assert "sentinel-secret-never-in-errors" not in str(caught.value)


def test_misplaced_role_key_is_not_logged_by_main_parser(tmp_path, monkeypatch, caplog):
    path = tmp_path / "misplaced-role-key.jsonc"
    data = {"default": _config().default.model_dump()}
    data["default"]["api_key"] = "sentinel-secret-never-in-logs"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(path))
    with pytest.raises(ValidationError) as caught:
        parse_llm_config()
    assert "api_key" in str(caught.value)
    assert "sentinel-secret-never-in-logs" not in str(caught.value)
    assert "sentinel-secret-never-in-logs" not in caplog.text


def test_explicit_main_path_and_overrides_fail_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(tmp_path / "missing.jsonc"))
    with pytest.raises(FileNotFoundError, match="ARTEMIS_ARTEMIS_JSONC"):
        parse_llm_config()
    monkeypatch.delenv("ARTEMIS_ARTEMIS_JSONC")
    with pytest.raises(FileNotFoundError):
        load_llm_config_override(tmp_path / "missing-profile.jsonc")
    invalid = tmp_path / "invalid.jsonc"
    invalid.write_text('{"nodes":{"operator":{"max_tokens":0}}}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_llm_config_override(invalid)


@pytest.mark.parametrize(
    "env_name",
    [
        "ARTEMIS_ARTEMIS_JSONC",
        "ARTEMIS_ARTEMIS_JSON",
        "ARTEMIS_LLM_CONFIG_JSON",
    ],
)
def test_all_explicit_model_paths_precede_repository_jsonc(tmp_path, monkeypatch, env_name):
    for name in ("ARTEMIS_ARTEMIS_JSONC", "ARTEMIS_ARTEMIS_JSON", "ARTEMIS_LLM_CONFIG_JSON"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "selected.json"
    path.write_text(json.dumps({"default": _config().default.model_dump()}), encoding="utf-8")
    monkeypatch.setenv(env_name, str(path))
    assert parse_llm_config().planner.provider == "minimax-prod"
    path.unlink()
    with pytest.raises(FileNotFoundError, match=env_name):
        parse_llm_config()


def test_main_selection_wins_over_mcp_stale_legacy(tmp_path, monkeypatch):
    from mcp_server.background.task_runner import resolve_profile_file

    (tmp_path / "llm-config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(tmp_path / "selected.jsonc"))
    assert resolve_profile_file() is None


def test_optional_minimax_roles_inherit_frozen_global_budget_not_operator(mini_registry):
    default = {
        "provider": "minimax-prod",
        "model": "default",
        "max_tokens": 12000,
        "timeout_seconds": 48,
        "fallback": {
            "provider": "minimax-prod",
            "model": "default",
            "max_tokens": 3000,
            "timeout_seconds": 21,
        },
    }
    config = _config(default, {"operator": {"max_tokens": 18000}, "planner_validation": None})
    assert config.planner_validation is None and config.validator_pixel_safety_net is None
    ctx = SimpleNamespace(llm_config=config)
    endpoint = get_effective_endpoint(ctx, "planner_validation")
    mini_registry.remove("minimax-prod")
    for role in (
        "history_analyzer",
        "validator_pixel_safety_net",
        "planner_validation",
        "output_analyzer",
    ):
        assert get_effective_endpoint(ctx, role).vendor == "minimax"
        assert get_effective_endpoint(ctx, role).max_tokens == 12000
        assert get_effective_endpoint(ctx, role).timeout_seconds == 48
        assert get_effective_endpoint(ctx, role, use_fallback=True).max_tokens == 3000
    assert endpoint.model_name == "MiniMax-M3.1-Flash"


def test_optional_node_profile_partial_fields_inherit_selected_default(mini_registry):
    config = deep_merge_llm_config(
        _config(), {"nodes": {"planner_validation": {"timeout_seconds": 55}}}
    )
    assert config.planner_validation.provider == "minimax-prod"
    endpoint = get_effective_endpoint(SimpleNamespace(llm_config=config), "planner_validation")
    assert endpoint.vendor == "minimax" and endpoint.timeout_seconds == 55


def test_snapshot_keeps_credentials_and_unrequested_roles_stable(mini_registry):
    config = _config(nodes={"operator": {"max_tokens": 12345}})
    ctx = SimpleNamespace(llm_config=config)
    first = get_effective_endpoint(ctx, "operator")
    mini_registry.remove("minimax-prod")
    config.planner.model = "changed-after-start"
    planner = get_effective_endpoint(ctx, "planner")
    assert planner.model_name == "MiniMax-M3.1-Flash"
    assert planner.api_key == first.api_key == "test-minimax-key"
    assert get_effective_endpoint(ctx, "operator").max_tokens == 12345
    assert get_effective_endpoint(ctx, "default").max_tokens == 8192
    assert get_effective_endpoint(ctx, "outputter", is_utils=True).api_key == first.api_key
    with pytest.raises(ValueError):
        get_effective_endpoint(SimpleNamespace(llm_config=config), "operator")


def test_snapshot_preflights_optional_fallbacks_and_rejects_mixed_vendors(mini_registry):
    config = _config(nodes={"output_analyzer": {"fallback": {"provider": "missing"}}})
    with pytest.raises(ValueError, match="missing"):
        get_effective_endpoint(SimpleNamespace(llm_config=config), "operator")
    mini_registry.add(name="other", api_key="k", api_base="https://other.invalid/v1", model="m")
    config = _config(nodes={"validator_pixel_safety_net": {"provider": "other", "model": "m"}})
    ctx = SimpleNamespace(llm_config=config)
    with pytest.raises(ValueError, match="mixed effective vendors"):
        context_uses_minimax(ctx)
    assert not getattr(ctx, "_llm_endpoint_snapshot", None)


def test_global_default_probe_and_legacy_baseline(mini_registry):
    config = _config(nodes={"operator": {"max_tokens": 20000}})
    assert get_effective_default_endpoint(config).max_tokens == 8192
    legacy = config.model_dump(exclude={"default"})
    flattened = LLMConfig.model_validate(_expand_default_into_nodes(legacy))
    assert get_effective_default_endpoint(flattened).max_tokens == 20000


def test_budget_timeout_and_zero_temperature_precedence(mini_registry):
    _register(mini_registry, timeout_seconds=120, temperature=0.7)
    config = _config(
        {
            "provider": "minimax-prod",
            "model": "default",
            "max_tokens": 10000,
            "timeout_seconds": 66,
            "fallback": {"provider": "minimax-prod", "model": "default", "max_tokens": 4000},
        },
        {"operator": {"max_tokens": 15000, "timeout_seconds": 77}},
    )
    ctx = SimpleNamespace(llm_config=config)
    assert get_effective_endpoint(ctx, "operator").max_tokens == 15000
    assert get_effective_endpoint(ctx, "operator").timeout_seconds == 77
    assert get_effective_endpoint(ctx, "planner").timeout_seconds == 66
    fallback = get_effective_endpoint(ctx, "operator", use_fallback=True)
    assert fallback.max_tokens == 4000
    assert fallback.timeout_seconds == 120


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_flash_constructor_has_explicit_transport_and_native_effort(
    mini_registry, monkeypatch, effort
):
    from artemis.llm import minimax_media

    constructor = Mock()
    monkeypatch.setattr(minimax_media, "MiniMaxChatAnthropic", constructor)
    endpoint = get_effective_endpoint(
        SimpleNamespace(
            llm_config=_config(
                nodes={
                    "operator": {
                        "reasoning_effort": effort,
                        "thinking_budget": 1234,
                        "thinking_level": "high",
                        "enable_grounding": True,
                        "max_tokens": 11000,
                        "timeout_seconds": 42,
                    }
                }
            )
        ),
        "operator",
    )
    ModelFactory.create_model(endpoint)
    assert constructor.call_args.kwargs == {
        "model": "MiniMax-M3.1-Flash",
        "api_key": "test-minimax-key",
        "base_url": "https://mini.invalid/anthropic",
        "max_tokens": 11000,
        "timeout": 42,
        "max_retries": 0,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }


@pytest.mark.parametrize("params", [{"reasoning_effort": "none"}, {"thinking_mode": "disabled"}])
def test_flash_invalid_thinking_effort_preflight(mini_registry, params):
    with pytest.raises(ValidationError):
        get_effective_endpoint(
            SimpleNamespace(llm_config=_config(nodes={"operator": params})), "operator"
        )


def test_m3_and_unknown_models_do_not_inherit_flash_effort():
    kwargs = {
        "provider": "anthropic",
        "vendor": "minimax",
        "api_key": "k",
        "api_base": "https://mini.invalid",
        "model_name": "MiniMax-M3",
    }
    assert ModelEndpoint(**kwargs).thinking_mode == "disabled"
    assert ModelEndpoint(**kwargs).max_tokens == 8192
    assert ModelEndpoint(**kwargs, thinking_mode="adaptive").reasoning_effort is None
    with pytest.raises(ValidationError, match="not verified"):
        ModelEndpoint(**kwargs, reasoning_effort="high")
    kwargs["model_name"] = "future-model"
    with pytest.raises(ValidationError, match="not verified"):
        ModelEndpoint(**kwargs, thinking_mode="adaptive")


def test_minimax_never_reads_global_anthropic_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-global-key")
    with pytest.raises(ValidationError, match="explicit endpoint"):
        ModelEndpoint(
            provider="anthropic",
            vendor="minimax",
            api_base="https://mini.invalid",
            model_name="MiniMax-M3.1-Flash",
        )


@pytest.mark.asyncio
async def test_actual_langchain_anthropic_wire_uses_resolved_limits(mini_registry, monkeypatch):
    import langchain_anthropic.chat_models as anthropic_module

    requests = []
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-token-must-not-leak")

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "MiniMax-M3.1-Flash",
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            anthropic_module, "_get_default_async_httpx_client", lambda **kwargs: client
        )
        endpoint = get_effective_endpoint(
            SimpleNamespace(
                llm_config=_config(nodes={"operator": {"max_tokens": 6000, "timeout_seconds": 37}})
            ),
            "operator",
        )
        model = ModelFactory.create_model(endpoint)
        response = await model.ainvoke("hello")
    assert response.content == "hello"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://mini.invalid/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "test-minimax-key"
    assert "authorization" not in request.headers
    assert request.extensions["timeout"]["read"] == 37
    payload = json.loads(request.content)
    assert payload["max_tokens"] == 6000
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["thinking"] == {"type": "adaptive"}
    assert "temperature" not in payload
    assert model.max_retries == 0


def test_minimax_grounding_remains_an_app_level_switch(mini_registry, monkeypatch):
    from artemis.llm import minimax_media

    endpoint = get_effective_endpoint(
        SimpleNamespace(
            llm_config=_config(
                nodes={
                    "operator": {"enable_grounding": True},
                }
            )
        ),
        "operator",
    )
    assert endpoint.enable_grounding is True
    constructor = Mock()
    monkeypatch.setattr(minimax_media, "MiniMaxChatAnthropic", constructor)
    ModelFactory.create_model(endpoint)
    assert "enable_grounding" not in constructor.call_args.kwargs


def test_endpoint_identity_isolates_host_vendor_and_credential_revision():
    base = ModelEndpoint(
        provider="anthropic", model_name="m", api_key="key1", api_base="https://one.invalid"
    )
    keys = {
        base.identity_key(),
        base.model_copy(update={"api_base": "https://two.invalid"}).identity_key(),
        base.model_copy(update={"api_key": "key2"}).identity_key(),
        base.model_copy(update={"vendor": "minimax"}).identity_key(),
    }
    assert len(keys) == 4
    assert "key1" not in str(keys) and "key2" not in str(keys)


def test_builtin_credentials_and_host_are_pinned(no_default_llm_provider, monkeypatch):
    from artemis.config import settings

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "first-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://first.invalid")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key-for-legacy-judges")
    config = _config(
        {
            "provider": "anthropic",
            "model": "claude-test",
            "fallback": {"provider": "anthropic", "model": "claude-fallback"},
        },
        {"operator": {"temperature": 0.0}},
    )
    ctx = SimpleNamespace(llm_config=config)
    endpoint = get_effective_endpoint(ctx, "operator")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "second-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://second.invalid")
    pinned = get_effective_endpoint(ctx, "planner")
    assert pinned.vendor is None
    assert pinned.api_key == endpoint.api_key == "first-key"
    assert pinned.api_base == "https://first.invalid"
    assert endpoint.temperature == 0.0


def test_openai_only_legacy_task_does_not_require_unused_google_judge_keys(
    no_default_llm_provider,
    monkeypatch,
):
    from artemis.config import settings

    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"):
        monkeypatch.setattr(settings, name, None)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.setenv("OPENAI_API_KEY", "only-openai-key")
    config = _config(
        {
            "provider": "openai",
            "model": "gpt-own",
            "fallback": {"provider": "openai", "model": "gpt-fallback"},
        }
    )
    ctx = SimpleNamespace(llm_config=config)
    endpoint = get_effective_endpoint(ctx, "operator")
    assert endpoint.provider == ModelProvider.OPENAI
    assert endpoint.api_key == "only-openai-key"
    assert not context_uses_minimax(ctx)
    with pytest.raises(ValueError, match="google.*no configured API key"):
        get_effective_endpoint(ctx, "planner_validation")
    # Adding a key later must not change a task's pinned missing-key state.
    monkeypatch.setenv("GOOGLE_API_KEY", "later-google-key")
    with pytest.raises(ValueError, match="google.*no configured API key"):
        get_effective_endpoint(ctx, "planner_validation")
    new_ctx = SimpleNamespace(llm_config=config)
    assert get_effective_endpoint(new_ctx, "planner_validation").api_key == "later-google-key"


def test_minimax_still_preflights_unused_fallback_credentials(mini_registry, monkeypatch):
    from artemis.config import settings

    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"):
        monkeypatch.setattr(settings, name, None)
        monkeypatch.delenv(name, raising=False)
    mini_registry.add(
        name="disabled-default", api_key="unused-key", enabled=False, make_default=True
    )
    config = _config(
        nodes={"output_analyzer": {"fallback": {"provider": "google", "model": "gemini-own"}}}
    )
    ctx = SimpleNamespace(llm_config=config)
    with pytest.raises(ValueError, match="google.*no configured API key"):
        get_effective_endpoint(ctx, "operator")
    assert not getattr(ctx, "_llm_endpoint_snapshot", None)


def test_local_and_vertex_endpoints_are_never_substituted(mini_registry):
    for provider in ("ollama", "vllm", "vertexai"):
        resolved = resolve_model_config(provider, "own-model")
        assert resolved.provider == provider
        assert resolved.model == "own-model"
        assert resolved.vendor is None


def test_disabled_default_restores_builtin_but_explicit_alias_fails(mini_registry):
    mini_registry.add(name="minimax-prod", api_key=None, enabled=False)
    assert resolve_model_config("google", "gemini-own").provider == "google"
    with pytest.raises(ValueError):
        resolve_model_config("default", "default")


def test_persisted_v1_provider_remains_readable(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_provider": "old",
                "providers": [
                    {
                        "name": "old",
                        "api_key": "old-key",
                        "api_base": "https://old.invalid/v1",
                        "model": "m",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    entry = LLMProviderRegistry(path).default_provider()
    assert entry.kind == "openai" and entry.vendor is None
    assert entry.api_key.get_secret_value() == "old-key"


def test_registry_secret_edit_enabled_and_explicit_clear(tmp_path):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    with pytest.raises(ValueError, match="required"):
        registry.add(name="new", api_key=None)
    registry.add(name="new", api_key="old-key", enabled=False)
    for key in (None, "", "   "):
        edited = registry.add(name="new", api_key=key, model="new-model")
        assert edited.api_key.get_secret_value() == "old-key"
        assert edited.enabled is False
    assert registry.add(name="new", api_key=None, enabled=True).enabled
    registry.add(name="new", clear_api_key=True)
    assert registry.get("new").api_key.get_secret_value() == ""


def test_registry_failed_replace_rolls_back_and_cleans_unique_temp(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    registry.add(name="old", api_key="old-key")
    previous = registry.path.read_bytes()

    def fail(*args, **kwargs):
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(PermissionError):
        registry.add(name="new", api_key="new-key")
    assert registry.path.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_registry_permission_failure_precedes_secret_write(tmp_path, monkeypatch):
    registry = LLMProviderRegistry(tmp_path / "providers.json")
    registry.add(name="old", api_key="old-key")
    previous = registry.path.read_bytes()

    def fail(path):
        assert path.read_bytes() == b""
        raise PermissionError("simulated permission protection failure")

    monkeypatch.setattr(providers, "_protect_registry_file", fail)
    with pytest.raises(PermissionError, match="protection failure"):
        registry.add(name="new", api_key="new-key")
    assert registry.path.read_bytes() == previous
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL regression")
def test_windows_registry_current_user_dacl_before_write_and_after_replace(tmp_path, monkeypatch):
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    registry = LLMProviderRegistry(tmp_path / "providers.json")
    original = providers._protect_registry_file
    protected = []

    def protect(path):
        assert path.read_bytes() == b""
        sid = original(path)
        protected.append((path, sid))
        return sid

    monkeypatch.setattr(providers, "_protect_registry_file", protect)
    registry.add(name="secret", api_key="secret-test-key")
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    assert protected and protected[0][1] == current_sid
    descriptor = win32security.GetNamedSecurityInfo(
        str(registry.path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    control, _ = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl.GetAceCount() == 1
    assert dacl.GetAce(0) == (
        (win32security.ACCESS_ALLOWED_ACE_TYPE, 0),
        ntsecuritycon.FILE_ALL_ACCESS,
        current_sid,
    )


def _process_writer(path, prefix):
    registry = LLMProviderRegistry(Path(path))
    for index in range(4):
        registry.add(name=f"{prefix}-{index}", api_key=f"key-{index}")


def test_concurrent_registry_process_and_thread_writes_have_no_lost_updates(tmp_path):
    path = tmp_path / "providers.json"
    processes = [
        multiprocessing.get_context("spawn").Process(
            target=_process_writer, args=(str(path), f"p{i}")
        )
        for i in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=45)
        assert process.exitcode == 0

    def write(index):
        LLMProviderRegistry(path).add(name=f"thread-{index}", api_key="key")

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write, range(10)))
    assert len(LLMProviderRegistry(path).list()) == 22


@pytest.mark.asyncio
async def test_anthropic_discovery_headers_url_and_cursor(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        index = len(requests)
        return httpx.Response(
            200,
            json={
                "data": [{"id": f"model-{index}"}],
                "has_more": index == 1,
                "last_id": f"model-{index}",
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    models, endpoint = await discover_models(
        "https://mini.invalid/anthropic", "key", kind="anthropic"
    )
    assert endpoint == "https://mini.invalid/anthropic/v1/models"
    assert [model.id for model in models] == ["model-1", "model-2"]
    assert requests[1].url.params["after_id"] == "model-1"
    assert requests[0].headers["x-api-key"] == "key"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_discovery_repeated_cursor_is_bounded(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(
            200, json={"data": [{"id": "one"}], "has_more": True, "last_id": "one"}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(RuntimeError, match="repeated"):
        await discover_models("https://mini.invalid", "key", kind="anthropic")
    with pytest.raises(ValueError, match="unsupported"):
        await discover_models("https://mini.invalid", "key", kind="unknown")
