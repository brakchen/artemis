"""Pure MCP configuration selection shared by parent admission and detached worker.

Imports stay lazy so MCP tool metadata never imports the Artemis root.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from artemis.sdk.types.agent import AgentConfig

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def resolve_profile_file() -> str | None:
    """Resolves the LLM configuration profile across multiple locations."""
    # A selected main file owns model resolution; stale legacy profiles must
    # not override it. initialize_llm_config surfaces a missing explicit path.
    if any(
        os.getenv(name)
        for name in (
            "ARTEMIS_ARTEMIS_JSONC",
            "ARTEMIS_ARTEMIS_JSON",
            "ARTEMIS_LLM_CONFIG_JSON",
        )
    ):
        return None
    candidates = []
    artemis_config_dir = os.getenv("ARTEMIS_CONFIG_DIR")
    if artemis_config_dir:
        candidates.extend(
            [
                os.path.join(artemis_config_dir, "llm-config.override.jsonc"),
                os.path.join(artemis_config_dir, "llm-config.json"),
            ]
        )
    try:
        # Preserve the worker's repo-relative fallback when the configuration
        # package cannot be loaded. Imports remain lazy for metadata discovery.
        from artemis.config.paths import get_app_dir

        app_dir = str(get_app_dir())
        candidates.extend(
            [
                os.path.join(app_dir, "llm-config.override.jsonc"),
                os.path.join(app_dir, "llm-config.json"),
            ]
        )
    except (ImportError, OSError):
        pass

    candidates.extend(
        [
            os.path.join(PROJECT_ROOT, "llm-config.override.jsonc"),
            os.path.join(PROJECT_ROOT, "llm-config.json"),
            os.path.join(PROJECT_ROOT, "config", "llm-config.json"),
        ]
    )

    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


def build_task_config(
    *, verification_level: str | None = None, explorer_mode: str | None = None
) -> AgentConfig:
    """Build the worker's actual configuration without touching devices or clients."""
    from artemis.config import initialize_llm_config, settings
    from artemis.sdk.builders import Builders
    from artemis.sdk.types import AgentProfile

    try:
        profile_file = resolve_profile_file()
        profile = (
            AgentProfile(name="default", from_file=profile_file)
            if profile_file
            else AgentProfile(name="default", llm_config=initialize_llm_config())
        )
        builder = Builders.AgentConfig.with_default_profile(profile)
        if verification_level:
            builder.with_verification_level(verification_level)
        if explorer_mode:
            builder.with_explorer(pro_mode=explorer_mode)
        if settings.ADB_HOST:
            builder.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)
        return builder.build(validate_profiles=False)
    except ValueError:
        raise
    except Exception:
        raise ValueError("Task model configuration could not be prepared") from None


def prepare_task_config(
    model: str = "Pro", *, verification_level: str | None = None, explorer_mode: str | None = None
) -> SimpleNamespace:
    """The result contains private keys; only model_snapshot is public."""
    from artemis.sdk import Agent

    config = build_task_config(verification_level=verification_level, explorer_mode=explorer_mode)
    return Agent(config=config).prepare_task_config("flash" if model.lower() == "flash" else None)
