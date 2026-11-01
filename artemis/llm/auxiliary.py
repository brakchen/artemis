"""Auxiliary models derived from the task's canonical summarizer endpoint."""

from __future__ import annotations

from typing import Any

from artemis.llm.router import ModelEndpoint

_MODELS = {"minimax-m3", "minimax-m3.1-flash"}
_PARAMETERS = {"thinking_mode", "reasoning_effort", "max_tokens", "timeout_seconds"}


def derive_auxiliary_endpoint(
    endpoint: ModelEndpoint, config: Any = None, *, model_name: str | None = None
) -> ModelEndpoint:
    """Revalidate a supported override without resolving the registry again."""
    if endpoint.vendor != "minimax":
        return endpoint.model_copy(deep=True)
    extras = getattr(config, "model_extra", None) or {}
    unknown = set(extras) - _PARAMETERS
    if unknown:
        raise ValueError(f"Unsupported MiniMax auxiliary parameters: {', '.join(sorted(unknown))}")
    fields_set = getattr(config, "model_fields_set", set())
    requested = model_name or (config.model if "model" in fields_set else None)
    model = requested or endpoint.model_name
    if model.lower() not in _MODELS:
        raise ValueError(f"Unsupported MiniMax auxiliary model: {model}")
    if model.lower() == endpoint.model_name.lower():
        data = dict(endpoint.__dict__)
    else:
        data = {
            name: getattr(endpoint, name)
            for name in (
                "provider",
                "vendor",
                "registry_name",
                "api_base",
                "api_key",
                "max_tokens",
                "timeout_seconds",
                "is_multimodal",
            )
        }
    data.update(model_name=model, **extras)
    return ModelEndpoint(**data)
