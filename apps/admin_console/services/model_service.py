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

import json
import logging
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


class ModelService:
    """Service handling agent architecture profile detection and metadata formatting."""

    @staticmethod
    def _format_model(profile: str | None, endpoint: dict | None = None) -> dict[str, str]:
        arch_name = "Flash"
        if profile:
            value = str(profile).lower()
            arch_name = (
                "Pro"
                if "pro" in value
                else "Flash"
                if "flash" in value
                else str(profile).capitalize()
            )
        info = {
            "name": arch_name,
            "id": "unknown",
            "provider": "unknown",
            "architecture": f"ARTEMIS {arch_name}",
        }
        if endpoint is not None:
            for source, target in (
                ("model", "id"),
                ("kind", "provider"),
                ("vendor", "vendor"),
                ("registry", "registry"),
            ):
                value = endpoint.get(source)
                if isinstance(value, str):
                    info[target] = value
        return info

    @classmethod
    def get_active_model_info(cls, profile: str | None = None) -> dict[str, str]:
        """Resolve current configured operator read-only, without constructing a client."""
        try:
            from artemis.config import parse_llm_config, load_agent_config
            from artemis.llm.resolution import public_model_snapshot

            snapshot = public_model_snapshot(
                SimpleNamespace(
                    llm_config=parse_llm_config(),
                    agent_config=load_agent_config(),
                )
            )
            operator = next(
                row
                for row in snapshot["roles"]
                if row["role"] == "operator" and not row["fallback"]
            )
            return cls._format_model(profile, operator)
        except (ValueError, OSError, KeyError, StopIteration) as exc:
            logger.warning(
                "Could not resolve model configuration for display (%s)", type(exc).__name__
            )
            info = cls._format_model(profile)
            info["diagnostic"] = f"Invalid model configuration ({type(exc).__name__})"
            return info

    @classmethod
    def get_session_model_info(
        cls,
        row: dict[str, Any],
        profile: str | None = None,
        llm_trace_payloads: list[str] | None = None,
    ) -> dict[str, str]:
        """Display persisted task evidence; current configuration is irrelevant."""
        raw = row.get("device_info")
        try:
            device_info = json.loads(raw) if isinstance(raw, str) else raw
            snapshot = device_info.get("model_snapshot") if isinstance(device_info, dict) else None
            if isinstance(snapshot, dict):
                roles = snapshot.get("roles", [])
                for endpoint in roles if isinstance(roles, list) else []:
                    if (
                        isinstance(endpoint, dict)
                        and endpoint.get("role") == "operator"
                        and endpoint.get("fallback") is False
                    ):
                        return cls._format_model(
                            profile or snapshot.get("requested_profile"), endpoint
                        )
            # Old traces can carry direct model evidence. Never substitute a
            # current configured model for a historical unknown.
            for raw_trace in llm_trace_payloads or []:
                trace = json.loads(raw_trace) if isinstance(raw_trace, str) else raw_trace
                if isinstance(trace, dict) and isinstance(trace.get("model"), str):
                    return cls._format_model(
                        profile, {"model": trace["model"], "kind": trace.get("provider")}
                    )
        except (ValueError, TypeError):
            pass
        return cls._format_model(profile)

    @staticmethod
    def resolve_session_profile(
        row_dict: dict[str, Any],
        llm_trace_payloads: list[str] | None = None,
        running_profile: str | None = None,
        agent_names: list[str] | None = None,
    ) -> str | None:
        """Resolve the Artemis agent architecture profile ('flash' or 'pro') for a session."""
        sess_profile = None
        d_info_raw = row_dict.get("device_info")
        if d_info_raw:
            try:
                d_info = json.loads(d_info_raw) if isinstance(d_info_raw, str) else d_info_raw
                if isinstance(d_info, dict):
                    p = d_info.get("profile")
                    if p:
                        p_str = str(p).lower()
                        if "pro" in p_str:
                            return "pro"
                        elif "flash" in p_str:
                            return "flash"
            except (ValueError, TypeError):
                # Malformed device_info JSON: try the other profile sources.
                pass

        if (row_dict.get("status") == "running") and running_profile:
            p_str = str(running_profile).lower()
            if "pro" in p_str:
                return "pro"
            elif "flash" in p_str:
                return "flash"

        # Check Agent/Trace names - the most definitive indicator of architecture
        if agent_names:
            agent_names_lower = [str(a).lower() for a in agent_names]
            if any(
                name in ("planner", "validator", "summarizer", "operator", "checker", "diagnoser")
                for name in agent_names_lower
            ):
                return "pro"
            if any("flashrunner" in name for name in agent_names_lower):
                return "flash"

        # Check LLM traces for agent name
        if llm_trace_payloads:
            for tr_payload in llm_trace_payloads:
                if tr_payload:
                    try:
                        p_obj = (
                            json.loads(tr_payload) if isinstance(tr_payload, str) else tr_payload
                        )
                        if isinstance(p_obj, dict):
                            agent_name = str(p_obj.get("agent") or p_obj.get("name") or "").lower()
                            if "flashrunner" in agent_name:
                                return "flash"
                            if any(
                                x in agent_name
                                for x in ("planner", "validator", "operator", "checker")
                            ):
                                return "pro"
                    except (ValueError, TypeError):
                        # Malformed trace payload JSON: skip this trace.
                        pass

        return None


model_service = ModelService()
