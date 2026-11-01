"""Configuration ownership at the task boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from types import SimpleNamespace

from pydantic import BaseModel


def copy_task_config(config: BaseModel) -> Any:
    """Freeze typed data while retaining live LangChain callback capabilities.

    Pydantic's copy preserves nested ``model_fields_set``. Callback handlers
    and managers may own locks, executors and open files; only their list
    container belongs to the task, not those live capabilities.
    """
    callbacks = getattr(config, "graph_config_callbacks", None)
    shallow = config.model_copy()
    if "graph_config_callbacks" in type(config).model_fields:
        shallow.__dict__["graph_config_callbacks"] = None
    result = deepcopy(shallow)
    if "graph_config_callbacks" in type(config).model_fields:
        result.__dict__["graph_config_callbacks"] = (
            list(callbacks) if isinstance(callbacks, list) else callbacks
        )
    return result


def get_task_agent_config(ctx: Any = None) -> Any:
    """Read the task's config; standalone legacy callers load and pin once."""
    config = getattr(ctx, "agent_config", None) if ctx is not None else None
    if isinstance(config, BaseModel):
        return config
    if (
        ctx is not None
        and config is not None
        and config is getattr(ctx, "_task_agent_config_snapshot", None)
    ):
        return config
    if ctx is not None:
        from artemis.utils.vision import uses_minimax

        if uses_minimax(ctx):
            # Resolution installs typed defaults for a standalone MiniMax
            # context, rather than importing unrelated local Gemini settings.
            from artemis.config.agent import AgentGlobalConfig

            if getattr(ctx, "agent_config", None) is None:
                ctx.agent_config = AgentGlobalConfig()
            return ctx.agent_config
    from artemis.config import load_agent_config

    config = deepcopy(load_agent_config())
    if ctx is not None:
        ctx.agent_config = config
        # Generic legacy loader results also belong to this context. Track
        # the installed object, rather than mistaking every opaque injection
        # for a configuration or reloading it on the next consumer read.
        ctx._task_agent_config_snapshot = config
    return config


def explicit_aux_model(config: BaseModel) -> str | None:
    """An implicit legacy Gemini default must not override a pinned model."""
    model = getattr(config, "model", None)
    return model if "model" in config.model_fields_set and isinstance(model, str) else None


def prepare_selected_task_config(
    config: BaseModel, selected: Any, requested: str | None = None, *, registry: Any = None
) -> SimpleNamespace:
    """Prepare an already selected profile without any runtime side effects."""
    from artemis.llm.resolution import context_uses_minimax, public_model_snapshot

    prepared = SimpleNamespace(
        agent_config=copy_task_config(config),
        llm_config=selected.llm_config.model_copy(deep=True),
        selected_llm_profile=selected.name,
    )
    if registry is not None:
        prepared._llm_provider_registry = registry
    context_uses_minimax(prepared)
    prepared.model_snapshot = public_model_snapshot(prepared, requested_profile=requested)
    return prepared
