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

"""LLM & Multimodal Vision Credentials Readiness Probe."""

from typing import Any
from artemis.config import settings
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


class LLMCredentialsProbe(BaseProbe):
    """Probe verifying Gemini and multi-provider multimodal LLM credentials."""

    @property
    def probe_id(self) -> str:
        return "gemini_api_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return True

    def _mask_key(self, key_str: str) -> str:
        """Helper to safely mask an API credential for display."""
        if len(key_str) > 10:
            return f"{key_str[:6]}...{key_str[-4:]}"
        return "***"

    async def probe(self) -> ProbeResult:
        import os

        from artemis.config.settings import is_placeholder_key
        from artemis.llm.providers import KEY_REQUIRED_BUILTINS, active_default_provider

        gemini_key = settings.get_api_key("google")
        openai_key = settings.get_api_key("openai")
        claude_key = settings.get_api_key("anthropic")
        openrouter_key = settings.get_api_key("openrouter")
        xai_key = settings.get_api_key("xai")
        ocr_key = settings.get_api_key("ocr")

        configured_providers: list[dict[str, Any]] = []
        api_keys_map: dict[str, str] = {}
        if gemini_key and not is_placeholder_key(gemini_key):
            g_val = gemini_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "google",
                    "label": "Gemini",
                    "masked": self._mask_key(g_val),
                    "raw_key": g_val,
                    "key": g_val,
                }
            )
            api_keys_map["google"] = g_val
            api_keys_map["gemini"] = g_val
        if openai_key and not is_placeholder_key(openai_key):
            o_val = openai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openai",
                    "label": "ChatGPT",
                    "masked": self._mask_key(o_val),
                    "raw_key": o_val,
                    "key": o_val,
                }
            )
            api_keys_map["openai"] = o_val
        if claude_key and not is_placeholder_key(claude_key):
            c_val = claude_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "anthropic",
                    "label": "Claude",
                    "masked": self._mask_key(c_val),
                    "raw_key": c_val,
                    "key": c_val,
                }
            )
            api_keys_map["anthropic"] = c_val
        if openrouter_key and not is_placeholder_key(openrouter_key):
            or_val = openrouter_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openrouter",
                    "label": "OpenRouter",
                    "masked": self._mask_key(or_val),
                    "raw_key": or_val,
                    "key": or_val,
                }
            )
            api_keys_map["openrouter"] = or_val
        if xai_key and not is_placeholder_key(xai_key):
            x_val = xai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "xai",
                    "label": "xAI (Grok)",
                    "masked": self._mask_key(x_val),
                    "raw_key": x_val,
                    "key": x_val,
                }
            )
            api_keys_map["xai"] = x_val

        # Detect any custom model endpoints or environment variables defined in files
        for env_var, label, prov_id in [
            ("DEEPSEEK_API_KEY", "DeepSeek", "deepseek"),
            ("GROQ_API_KEY", "Groq", "groq"),
            ("OPENAI_BASE_URL", "Custom OpenAI Endpoint", "custom"),
            ("OLLAMA_BASE_URL", "Local Ollama", "ollama"),
            ("VLLM_BASE_URL", "vLLM Endpoint", "vllm"),
            ("VERTEX_AI_PROJECT", "Google Cloud Vertex AI", "vertexai"),
        ]:
            val = os.environ.get(env_var)
            if val and val.strip() and not is_placeholder_key(val.strip()):
                configured_providers.append(
                    {
                        "provider": prov_id,
                        "label": label,
                        "masked": self._mask_key(val.strip()),
                        "raw_key": val.strip(),
                        "key": val.strip(),
                    }
                )
                api_keys_map[prov_id] = val.strip()

        if ocr_key and not is_placeholder_key(ocr_key):
            api_keys_map["ocr"] = ocr_key.get_secret_value()

        # A registered default provider overrides every key-carrying built-in,
        # with or without its own key: it is the LLM that will actually run.
        # Built-in keys are then not consulted, so they must not fail live
        # verification either (they belong to a credential the runtime never
        # uses) — drop them from the verified set and report them as ignored.
        default_entry = active_default_provider()
        if default_entry is not None:
            metadata_keys = [
                p for p in configured_providers if p["provider"] in KEY_REQUIRED_BUILTINS
            ]
            configured_providers = [
                p for p in configured_providers if p["provider"] not in KEY_REQUIRED_BUILTINS
            ]
            for key_id in tuple(api_keys_map):
                if key_id in KEY_REQUIRED_BUILTINS:
                    api_keys_map.pop(key_id)
        else:
            metadata_keys = []

        current_active_key = (
            gemini_key.get_secret_value()
            if gemini_key
            else (configured_providers[0]["key"] if configured_providers else "")
        )

        metadata = {
            "configured_count": len(configured_providers),
            "providers": configured_providers,
            "has_ocr_key": ocr_key is not None,
            "current_key": current_active_key,
            "current_gemini_key": gemini_key.get_secret_value() if gemini_key else "",
            "api_keys": api_keys_map,
        }
        if default_entry is not None:
            metadata["default_provider"] = {
                "name": default_entry.name,
                "model": default_entry.model,
                "fallback_model": default_entry.fallback_model,
                "api_base": default_entry.api_base,
            }
            metadata["ignored_builtin_keys"] = [p["label"] for p in metadata_keys]
            # No built-in credential is in use, so none should be reported (or
            # redacted) as the active one.
            metadata["current_key"] = ""
            metadata["current_gemini_key"] = ""

        # Case 0: a registered default provider is active → it is the LLM for
        # every node, so no built-in key is required at all.
        if default_entry is not None:
            model_label = default_entry.model or "default model"
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Active (default provider '{default_entry.name}')",
                description=(
                    f"Custom provider '{default_entry.name}' is the default LLM "
                    f"({model_label} @ {default_entry.api_base or 'its endpoint'}) and overrides "
                    "built-in providers; no built-in API key is needed."
                ),
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Default Provider Active",
                        payload=(
                            f"'{default_entry.name}' serves every model node. "
                            "Manage it with `artemis providers list`."
                        ),
                    )
                ],
            )

        # Case 1: Gemini API Key configured (Standard / Recommended)
        if gemini_key:
            masked = self._mask_key(gemini_key.get_secret_value())
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="Active (Gemini)",
                description=f"Gemini multimodal API credential is active ({masked}) and ready for vision and reasoning.",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Provider Active",
                        payload="Gemini 2.5 Flash / Pro Multimodal Engine is enabled.",
                    )
                ],
            )

        # Case 2: Other LLM provider configured
        if configured_providers:
            first_p = configured_providers[0]
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Active ({first_p['label']})",
                description=f"{first_p['label']} multimodal API credential is active ({first_p['masked']}).",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Provider Active",
                        payload=f"{first_p['label']} multimodal vision endpoint is active.",
                    )
                ],
            )

        # Case 3: No LLM key configured
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Multimodal LLM API Key",
            status=ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary="Key Missing",
            description="No Multimodal LLM credential (e.g. GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY) found in environment or .env file.",
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="command",
                    label="Run Artemis Init",
                    payload="artemis init",
                ),
                ProbeAction(
                    action_type="link",
                    label="Get Google AI Studio Key",
                    payload="https://aistudio.google.com/app/apikey",
                ),
            ],
        )


class VisionOCRProbe(BaseProbe):
    """Probe verifying optional Google Cloud Vision OCR credentials."""

    @property
    def probe_id(self) -> str:
        return "vision_ocr_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return False

    def _mask_key(self, key_str: str) -> str:
        """Helper to safely mask an API credential for display."""
        if len(key_str) > 10:
            return f"{key_str[:6]}...{key_str[-4:]}"
        return "***"

    async def probe(self) -> ProbeResult:
        from artemis.utils.ocr_api import is_ocr_configured

        ocr_key = settings.get_api_key("ocr")
        is_configured = is_ocr_configured() and ocr_key is not None

        if is_configured and ocr_key:
            val = ocr_key.get_secret_value()
            masked = self._mask_key(val)
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Vision OCR API Key (Optional)",
                status=ProbeStatus.PASS,
                is_blocker=False,
                summary="Active & Configured",
                description=f"Google Cloud Vision OCR ({masked}) is active for image text recognition.",
                metadata={"configured": True, "masked_key": masked, "key": val, "raw_key": val},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="OCR Enabled",
                        payload="OCR is active and will fuse text with UI XML hierarchy.",
                    )
                ],
            )

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Vision OCR API Key (Optional)",
            status=ProbeStatus.PASS,
            is_blocker=False,
            summary="Not Configured (Optional)",
            description="OCR_API_KEY is not set. Perception uses the UI XML hierarchy without OCR.",
            metadata={"configured": False},
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="Standard XML Perception",
                    payload="Artemis uses pure UI layout parsing and Set-of-Marks visual grounding.",
                )
            ],
        )
