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

"""LLM provider, model hierarchy, fallback chaining, and configuration loaders."""

from pathlib import Path
from copy import deepcopy
import os
from typing import Any


from artemis.config.constants import (
    LLM_CONFIG_FILENAME,
    AgentNode,
)
from artemis.config.paths import get_config_path, get_main_config_path
from artemis.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.config import llm as base_llm_config
from third_party.mobile_use.config.llm import (
    LLM,
    AgentNodeWithFallback,
    LLMConfigBase,
    LLMConfigUtils,
    LLMUtilsNodeWithFallback,
    LLMWithFallback,
    validate_vertex_ai_credentials,
)
from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "LLM",
    "AgentNodeWithFallback",
    "LLMUtilsNodeWithFallback",
    "CyFunctionDetector",
    "LLMConfig",
    "LLMConfigUtils",
    "LLMWithFallback",
    "deep_merge_llm_config",
    "get_default_llm_config",
    "initialize_llm_config",
    "lightweight_judge_default",
    "load_llm_config_override",
    "parse_llm_config",
    "validate_vertex_ai_credentials",
]


def lightweight_judge_default() -> "LLMWithFallback":
    """Factory default for the lightweight judge nodes (pixel safety net and
    planner validation): a flash-lite model at temperature 0."""
    return LLMWithFallback(
        provider="google",
        model="gemini-3.5-flash-lite",
        temperature=0.0,
        fallback=LLM(
            provider="google",
            model="gemini-3.1-flash-lite",
            temperature=0.0,
        ),
    )


class LLMConfig(LLMConfigBase):
    """Comprehensive LLM configuration mapping every node to primary/fallback models."""

    summarizer: LLMWithFallback
    operator: LLMWithFallback
    operator_summarizer: LLMWithFallback
    log_reader_sub_agent: LLMWithFallback
    log_analyzer: LLMWithFallback
    diagnoser: LLMWithFallback
    checker: LLMWithFallback
    planner_avatar: LLMWithFallback
    history_analyzer_expert: LLMWithFallback
    diagnoser_expert: LLMWithFallback
    explorer: LLMWithFallback
    history_analyzer: LLMWithFallback | None = None
    validator_pixel_safety_net: LLMWithFallback | None = None
    planner_validation: LLMWithFallback | None = None
    output_analyzer: LLMWithFallback | None = None
    default: LLMWithFallback | None = None

    def validate_providers(self) -> None:
        """Validate credentials across all configured agent nodes."""
        super().validate_providers()
        self.summarizer.validate_provider("Summarizer")
        self.operator.validate_provider("Operator")
        self.operator_summarizer.validate_provider("OperatorSummarizer")
        self.log_reader_sub_agent.validate_provider("LogReaderSubAgent")
        self.log_analyzer.validate_provider("LogAnalyzer")
        self.diagnoser.validate_provider("Diagnoser")
        self.checker.validate_provider("Checker")
        self.planner_avatar.validate_provider("PlannerAvatar")
        self.history_analyzer_expert.validate_provider("HistoryAnalyzerExpert")
        self.diagnoser_expert.validate_provider("DiagnoserExpert")
        self.explorer.validate_provider("Explorer")
        if self.history_analyzer:
            self.history_analyzer.validate_provider("HistoryAnalyzer")
        if self.validator_pixel_safety_net:
            self.validator_pixel_safety_net.validate_provider("ValidatorPixelSafetyNet")
        if self.planner_validation:
            self.planner_validation.validate_provider("PlannerValidation")
        if self.output_analyzer:
            self.output_analyzer.validate_provider("OutputAnalyzer")

    def get_agent(self, item: AgentNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific agent node with sensible defaults."""
        val = getattr(self, item)
        if val is None:
            if item == "history_analyzer":
                return self.operator
            elif item in ("validator_pixel_safety_net", "planner_validation"):
                # Both are cheap, high-frequency judges: the pixel safety net
                # runs before actions, the planner validator after every
                # milestone edit. They share one lightweight default.
                return lightweight_judge_default()
            elif item == "output_analyzer":
                return self.log_analyzer
        return val


AGENT_ROLES = tuple(name for name in LLMConfig.model_fields if name not in {"utils", "default"})
OPTIONAL_AGENT_ROLES = tuple(
    name for name in AGENT_ROLES if LLMConfig.model_fields[name].default is None
)
UTILITY_ROLES = tuple(LLMConfigUtils.model_fields)


def _merge_dict(base: dict, override: dict) -> dict:
    if "timeout" in override:
        override = deepcopy(override)
        timeout = override.pop("timeout")
        if "timeout_seconds" in override and override["timeout_seconds"] != timeout:
            raise ValueError("timeout and timeout_seconds conflict")
        override["timeout_seconds"] = timeout
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _expand_default_into_nodes(config_dict: dict, *, partial: bool = False) -> dict:
    """Normalize both formats; partial profiles contribute only explicit values.

    Top-level agent/device sections belong to other config owners. Model/role
    fields stay strict, including optional roles and utility names.
    """
    if not isinstance(config_dict, dict):
        raise ValueError("LLM configuration must be an object")
    unrelated = {"agent", "device", "devices", "memory", "presets", "version"}
    data = {k: deepcopy(v) for k, v in config_dict.items() if k not in unrelated}
    unknown = set(data) - set(AGENT_ROLES) - {"default", "nodes", "utils"}
    if unknown:
        raise ValueError(f"Unknown LLM configuration roles: {sorted(unknown)}")
    if "nodes" not in data and "default" not in data:
        return data
    default = data.pop("default", {})
    nodes = data.pop("nodes", {})
    if not isinstance(default, dict) or not isinstance(nodes, dict):
        raise ValueError("default and nodes must be objects")
    unknown = set(nodes) - set(AGENT_ROLES) - set(UTILITY_ROLES)
    if unknown:
        raise ValueError(f"Unknown LLM nodes: {sorted(unknown)}")
    if not partial and not default:
        raise ValueError("Unified LLM configuration requires a default model")
    result: dict[str, Any] = {"default": deepcopy(default)} if default else {}
    for role in AGENT_ROLES:
        # Preserve legacy get_agent defaults when no role override is selected.
        # A MiniMax task inherits the global model at the pinned snapshot boundary.
        if role in OPTIONAL_AGENT_ROLES and role not in nodes:
            continue
        if not partial or default or role in nodes:
            result[role] = (
                None if nodes.get(role, {}) is None else _merge_dict(default, nodes.get(role, {}))
            )
    utils = {}
    for role in UTILITY_ROLES:
        if not partial or default or role in nodes:
            utils[role] = (
                None if nodes.get(role, {}) is None else _merge_dict(default, nodes.get(role, {}))
            )
    if utils:
        result["utils"] = utils
    return _merge_dict(result, data)


def parse_llm_config() -> LLMConfig:
    """Parse and instantiate LLMConfig from artemis.jsonc or llm-config.json."""
    config_path = get_main_config_path(LLM_CONFIG_FILENAME)

    try:
        with open(config_path, encoding="utf-8") as f:
            config_dict = load_jsonc(f)
            expanded_dict = _expand_default_into_nodes(config_dict)
            return LLMConfig.model_validate(expanded_dict)
    except Exception as e:
        logger.error(f"Failed to load or parse llm config: {config_path}. Error: {e}")
        raise


def initialize_llm_config() -> LLMConfig:
    """Parse and validate credentials for LLMConfig."""
    return base_llm_config.initialize_llm_config(parse_llm_config)


def get_default_llm_config() -> LLMConfig:
    """Returns default LLMConfig parsed from standard configuration file."""
    return parse_llm_config()


def deep_merge_llm_config(base: LLMConfig, overrides: dict) -> LLMConfig:
    """Recursively merge dictionary overrides into an existing LLMConfig object."""
    normalized = _expand_default_into_nodes(overrides, partial=True)
    base_dict = base.model_dump()
    if base_dict["default"] is None and isinstance(normalized.get("default"), dict):
        # Flattened legacy files use Operator as the global baseline, matching
        # endpoint resolution. Partial profiles must not create an incomplete LLM.
        base_dict["default"] = base.operator.model_dump()
    for role in OPTIONAL_AGENT_ROLES:
        if base_dict[role] is None and isinstance(normalized.get(role), dict):
            base_dict[role] = (base.default or base.get_agent(role)).model_dump()
    return LLMConfig.model_validate(_merge_dict(base_dict, normalized))


def load_llm_config_override(path: Path | str) -> LLMConfig:
    """Load custom LLM configuration JSON/JSONC overrides on top of default configuration."""
    resolved_path = Path(path)
    if not resolved_path.exists():
        try:
            resolved_path = get_config_path(str(path))
        except OSError:
            pass
    return base_llm_config.load_llm_config_override(
        resolved_path, get_default_llm_config, deep_merge_llm_config
    )
