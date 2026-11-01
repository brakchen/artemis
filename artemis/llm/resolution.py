"""Task-pinned effective endpoints shared by model, media and native helpers.

Call ``get_effective_endpoint(ctx, "default")`` for utilities that inherit the
global model rather than an agent override. Legacy flattened configs use the
operator as their global baseline. ``context_uses_minimax`` performs the same
preflight without constructing a client. No raw credentials enter public dumps.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from artemis.config import settings
from artemis.config.llm import AGENT_ROLES, UTILITY_ROLES, LLMConfig
from artemis.llm import providers
from artemis.llm.router import ModelEndpoint, ModelProvider


def _value(config: Any, name: str, default: Any = None) -> Any:
    value = getattr(config, name, None)
    return default if value is None else value


def _endpoint(
    config: Any, global_config: Any, registry: providers.LLMProviderRegistry, *, fallback: bool
) -> ModelEndpoint:
    resolved = providers.resolve_model_config(
        config.provider,
        config.model,
        use_fallback=fallback,
        registry=registry,
    )
    entry = resolved.entry
    provider = ModelProvider.from_string(resolved.provider)
    api_key = resolved.api_key
    api_base = resolved.api_base
    if entry is None:
        key = settings.get_api_key(provider.value)
        api_key = key.get_secret_value() if key is not None else None
        if api_key is None:
            for env_name in providers._BUILTIN_ENV_KEYS.get(str(config.provider), ()):
                api_key = os.getenv(env_name) or None
                if api_key:
                    break
        if provider in {ModelProvider.OLLAMA, ModelProvider.VLLM, ModelProvider.CUSTOM}:
            api_key = os.getenv("OPENAI_API_KEY") or "EMPTY"
            api_base = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    if provider == ModelProvider.OPENAI and api_base is None:
        api_base = (
            str(settings.OPENAI_BASE_URL)
            if settings.OPENAI_BASE_URL
            else (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1")
        )
    if provider == ModelProvider.ANTHROPIC and api_base is None and resolved.vendor != "minimax":
        api_base = (
            os.getenv("ANTHROPIC_API_URL")
            or os.getenv("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        )
    timeout = _value(config, "timeout_seconds", _value(config, "timeout"))
    if timeout is None:
        timeout = _value(global_config, "timeout_seconds", _value(global_config, "timeout"))
    if timeout is None and entry is not None:
        timeout = entry.timeout_seconds
    max_tokens = _value(config, "max_tokens", _value(global_config, "max_tokens"))
    return ModelEndpoint(
        provider=provider,
        model_name=resolved.model,
        api_key=api_key,
        api_base=api_base,
        vendor=resolved.vendor,
        registry_name=resolved.registry_name,
        max_tokens=max_tokens,
        timeout_seconds=60.0 if timeout is None else timeout,
        temperature=_value(
            config,
            "temperature",
            entry.temperature if entry and entry.temperature is not None else 0.0,
        ),
        is_multimodal=entry.is_multimodal if entry else True,
        thinking_mode=_value(config, "thinking_mode"),
        reasoning_effort=_value(config, "reasoning_effort"),
        thinking_budget=_value(config, "thinking_budget"),
        thinking_level=_value(config, "thinking_level"),
        include_thoughts=_value(config, "include_thoughts"),
        enable_grounding=_value(config, "enable_grounding", False),
    )


def _require_credentials(endpoint: ModelEndpoint) -> None:
    """Reject a pinned missing key when the endpoint is about to be used."""
    if endpoint.provider.value in providers.KEY_REQUIRED_BUILTINS and not endpoint.api_key:
        raise ValueError(
            f"LLM provider '{endpoint.registry_name or endpoint.provider.value}' has no configured API key"
        )


def _snapshot(ctx: Any, role: str, is_utils: bool) -> dict[tuple[str, bool, bool], ModelEndpoint]:
    existing = getattr(ctx, "_llm_endpoint_snapshot", None)
    if isinstance(existing, dict) and existing:
        return existing
    config = ctx.llm_config
    if config is None:
        from artemis.config.llm import get_default_llm_config

        config = get_default_llm_config()
        ctx.llm_config = config
    registry = providers.provider_registry.snapshot()
    if isinstance(config, LLMConfig):
        config = config.model_copy(deep=True)
        global_config = config.default or config.operator
        global_is_minimax = (
            providers.resolve_model_config(
                global_config.provider,
                global_config.model,
                registry=registry,
            ).vendor
            == "minimax"
        )
        selected = [
            (
                name,
                False,
                global_config
                if getattr(config, name) is None and global_is_minimax
                else config.get_agent(name),
            )
            for name in AGENT_ROLES
        ]
        selected.extend(
            (name, True, getattr(config.utils, name))
            for name in UTILITY_ROLES
            if getattr(config.utils, name) is not None
        )
        selected.append(("default", False, global_config))
    else:
        # Small SDK/test contexts expose only one requested role. Real tasks
        # always carry LLMConfig and receive the complete primary/fallback preflight.
        global_config = getattr(config, "default", None)
        selected = [
            (role, is_utils, config.get_utils(role) if is_utils else config.get_agent(role))
        ]
    result: dict[tuple[str, bool, bool], ModelEndpoint] = {}
    for name, utility, model_config in selected:
        if model_config is None:
            continue
        result[(name, utility, False)] = _endpoint(
            model_config, global_config, registry, fallback=False
        )
        fallback = getattr(model_config, "fallback", None)
        if fallback is not None:
            global_fallback = getattr(global_config, "fallback", None)
            result[(name, utility, True)] = _endpoint(
                fallback, global_fallback, registry, fallback=True
            )
    if any(endpoint.vendor == "minimax" for endpoint in result.values()):
        # MiniMax tasks preflight every active endpoint. Legacy tasks still pin
        # missing-key state for unused built-ins without requiring those vendors.
        for endpoint in result.values():
            _require_credentials(endpoint)
        mixed = sorted(
            {name for (name, _, _), endpoint in result.items() if endpoint.vendor != "minimax"}
        )
        if mixed:
            raise ValueError(
                f"MiniMax task has mixed effective vendors in roles: {', '.join(mixed)}"
            )
    # Publish only after all endpoints pass, so failed preflight cannot pin half a task.
    ctx._llm_endpoint_snapshot = result
    return result


def get_effective_endpoint(
    ctx: Any, role: str, *, is_utils: bool = False, use_fallback: bool = False
) -> ModelEndpoint:
    """Return a defensive copy of the task's pinned, fully resolved endpoint."""
    endpoints = _snapshot(ctx, role, is_utils)
    key = (role, is_utils, use_fallback)
    if key not in endpoints:
        raise ValueError(
            f"LLM configuration for '{role}' has no {'fallback' if use_fallback else 'endpoint'}"
        )
    endpoint = endpoints[key]
    _require_credentials(endpoint)
    return endpoint.model_copy(deep=True)


def context_uses_minimax(ctx: Any) -> bool:
    """Preflight a task and report its effective vendor for native integrations."""
    return any(
        endpoint.vendor == "minimax" for endpoint in _snapshot(ctx, "default", False).values()
    )


def get_effective_default_endpoint(config: LLMConfig | None = None) -> ModelEndpoint:
    """Read-only server-side default probe, without caching or constructing a client.

    Its credential is available only to server-side callers; use an explicit
    allowlist for public views (api_key is excluded from repr/model_dump).
    """
    return get_effective_endpoint(SimpleNamespace(llm_config=config), "default")
