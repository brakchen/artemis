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

"""Credential presence only. Public probe results never contain raw credentials."""

from __future__ import annotations

from artemis.config import settings
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import ProbeAction, ProbeCategory, ProbeResult, ProbeStatus


class LLMCredentialsProbe(BaseProbe):
    @property
    def probe_id(self) -> str:
        return "gemini_api_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        from artemis.llm.resolution import get_effective_default_endpoint

        providers = [
            {"provider": name, "label": label, "configured": True, "verified": False}
            for name, label in (
                ("google", "Gemini"),
                ("openai", "ChatGPT"),
                ("anthropic", "Claude"),
                ("openrouter", "OpenRouter"),
                ("xai", "xAI (Grok)"),
            )
            if settings.get_api_key(name) is not None
        ]
        metadata = {
            "configured_count": len(providers),
            "providers": providers,
            "has_ocr_key": settings.get_api_key("ocr") is not None,
            "configured": False,
            "verified": False,
        }
        try:
            endpoint = get_effective_default_endpoint()
        except (ValueError, OSError):
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.FAIL,
                is_blocker=True,
                summary="Configuration Required",
                description="The selected model configuration or credential is missing or invalid. Configure the selected provider with artemis init or provider settings.",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="command", label="Run Artemis Init", payload="artemis init"
                    )
                ],
            )
        selected = {
            "provider": endpoint.provider.value,
            "label": endpoint.registry_name or endpoint.vendor or endpoint.provider.value,
            "configured": True,
            "verified": False,
        }
        metadata.update(configured=True, selected_provider=selected)
        if endpoint.registry_name:
            metadata["default_provider"] = {
                "name": endpoint.registry_name,
                "model": endpoint.model_name,
            }
            metadata["ignored_builtin_keys"] = [entry["label"] for entry in providers]
            metadata["providers"] = [selected]
            metadata["configured_count"] = 1
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Multimodal LLM API Key",
            status=ProbeStatus.PASS,
            is_blocker=True,
            summary=f"Configured ({selected['label']}; not verified)",
            description="The selected default endpoint is configured. Authentication and model capabilities have not been verified; readiness polling makes no model request.",
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="Provider Configured",
                    payload="Use explicit credential verification to check catalog authentication; task capabilities require separate verification.",
                )
            ],
        )


class VisionOCRProbe(BaseProbe):
    @property
    def probe_id(self) -> str:
        return "vision_ocr_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return False

    async def probe(self) -> ProbeResult:
        from artemis.config import load_agent_config
        from artemis.llm.resolution import get_effective_default_endpoint

        endpoint = None
        try:
            endpoint = get_effective_default_endpoint()
        except (ValueError, OSError):
            pass
        minimax = endpoint is not None and endpoint.vendor == "minimax"
        configured = minimax or settings.get_api_key("ocr") is not None
        automatic = None
        if minimax:
            automatic = load_agent_config().ocr.automatic
            automatic = automatic if type(automatic) is bool else False
        disabled = minimax and not automatic
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Vision OCR (Optional)",
            status=ProbeStatus.PASS,
            is_blocker=False,
            summary=(
                "Automatic OCR Disabled"
                if disabled
                else "Configured (not verified)"
                if configured
                else "Not Configured (Optional)"
            ),
            description=(
                "MiniMax OCR uses the selected default endpoint; no independent Google Vision key is required. Automatic OCR is disabled; explicit OCR remains available."
                if disabled
                else "OCR credentials are configured; no recognition or authentication request was performed."
                if configured
                else "OCR_API_KEY is not set. Perception uses the UI XML hierarchy without OCR."
            ),
            metadata={
                "configured": configured,
                "verified": False,
                "disabled": disabled,
                "source": "default_endpoint" if minimax else "google_vision",
                "automatic": automatic,
            },
        )
