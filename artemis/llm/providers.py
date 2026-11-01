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

"""User-configured LLM provider registry.

Lets operators register named model providers (OpenAI-compatible endpoints:
``base_url`` + ``api_key`` + optional default ``model``), keep several of them
side by side, and pick one as the default provider used whenever a model config
does not name an explicit provider.

Storage: ``<app dir>/llm_providers.json`` (outside the source checkout so API
keys never land in the git tree). See ``LLMProviderRegistry`` for the schema.

Resolution order used by :func:`resolve_provider_for_model`:

1. an explicit registry entry name (``"my-proxy"``) → that entry;
2. ``"default"`` / ``"auto"`` / empty → the registry default provider;
3. a key-carrying built-in provider (google/openai/anthropic/...) → replaced by
   the registry default provider **whenever one is set**, whether or not the
   built-in has its own API key (source ``"default"``, or ``"fallback"`` when
   the built-in was credential-less). That is the "as long as a default is
   set, every node uses the default LLM" rule: operators who register a custom
   provider never need ``GOOGLE_API_KEY``/``OPENAI_API_KEY`` in ``.env``;
4. anything else (local ``ollama``/``vllm``, ``vertexai`` ADC, unknown names)
   → untouched.

Removing the default (``artemis providers remove <name>`` when it is the last
one, or registering another one as default) restores built-in behaviour.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from artemis.config.paths import get_app_dir
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

PROVIDERS_FILENAME = "llm_providers.json"

#: Transport kind. ``openai`` (and its alias ``openai_compatible``) speaks the
#: OpenAI chat-completions wire format and is what custom gateways usually use.
SUPPORTED_KINDS: tuple[str, ...] = ("openai", "openai_compatible", "anthropic")

#: Names that must stay reserved so a registry entry can never shadow or be
#: confused with a built-in provider.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "google",
        "gemini",
        "vertexai",
        "vertex",
        "openai",
        "anthropic",
        "claude",
        "openrouter",
        "xai",
        "grok",
        "ollama",
        "vllm",
        "custom",
        "ocr",
        "vision",
        "google_vision",
        "default",
        "auto",
    }
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _protect_registry_file(path: Path) -> Any:
    """Restrict a still-empty secret file before writing, returning its Windows SID."""
    if os.name != "nt":
        path.chmod(0o600)
        return None
    # pywin32's maintained WinAPI bindings (PSF license), using the documented
    # protected DACL API: https://mhammond.github.io/pywin32/win32security__SetNamedSecurityInfo_meth.html
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    _verify_registry_file_permissions(path, user_sid)
    return user_sid


def _verify_registry_file_permissions(path: Path, user_sid: Any) -> None:
    """Fail closed if a temporary or published registry permits another principal."""
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise PermissionError("LLM provider registry must be owner-only")
        return
    import ntsecuritycon
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    control, _ = descriptor.GetSecurityDescriptorControl()
    if not control & win32security.SE_DACL_PROTECTED or dacl is None or dacl.GetAceCount() != 1:
        raise PermissionError("LLM provider registry requires a protected current-user DACL")
    (ace_type, flags), mask, allowed_sid = dacl.GetAce(0)
    if (
        ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE
        or flags != 0
        or mask != ntsecuritycon.FILE_ALL_ACCESS
        or allowed_sid != user_sid
    ):
        raise PermissionError("LLM provider registry DACL is not restricted to the current user")


#: Built-in providers that require an API key to be usable at all. Used for the
#: "configured provider is missing a key → fall back to the default provider"
#: rule.
KEY_REQUIRED_BUILTINS: frozenset[str] = frozenset(
    {"google", "gemini", "openai", "anthropic", "claude", "openrouter", "xai", "grok"}
)

#: Environment variables that satisfy each key-carrying built-in. Mirrors
#: ``settings.get_api_key`` but also sees variables exported after import.
_BUILTIN_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GCP_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPEN_ROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "grok": ("XAI_API_KEY",),
}


class LLMProvider(BaseModel):
    """A named, user-registered LLM endpoint."""

    model_config = {"extra": "forbid", "hide_input_in_errors": True}
    name: str = Field(description="Unique provider name, e.g. 'my-proxy'")
    kind: str = Field(default="openai", description="Wire format: openai (OpenAI-compatible)")
    vendor: Literal["minimax"] | None = None
    api_base: str | None = Field(
        default=None,
        description="Base URL of an OpenAI-compatible endpoint, e.g. https://api.example.com/v1",
    )
    api_key: SecretStr = Field(description="API key sent as Bearer token")
    model: str | None = Field(
        default=None, description="Default model name for this provider, e.g. gpt-4o"
    )
    fallback_model: str | None = Field(
        default=None,
        description="Fallback model name, mirrors artemis.jsonc default.fallback.model",
    )
    temperature: float | None = Field(default=None, description="Optional sampling temperature")
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    is_multimodal: bool = Field(default=True, description="Whether image inputs are supported")
    enabled: bool = Field(default=True, description="Disabled entries are ignored at resolution")

    @model_validator(mode="after")
    def _validate_vendor(self) -> LLMProvider:
        if self.vendor == "minimax" and self.kind != "anthropic":
            raise ValueError("vendor=minimax requires kind=anthropic")
        return self

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = value.strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError(
                "provider name must be 1-64 chars of lowercase letters/digits/._- "
                "and start with a letter or digit"
            )
        if name in RESERVED_NAMES:
            raise ValueError(f"'{name}' is reserved for a built-in provider")
        return name

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        kind = value.strip().lower().replace("-", "_")
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported provider kind {value!r}; supported: {SUPPORTED_KINDS}")
        return "openai" if kind == "openai_compatible" else kind

    @field_validator("api_base")
    @classmethod
    def _validate_api_base(cls, value: str | None) -> str | None:
        if value is None:
            return None
        base = value.strip().rstrip("/")
        if not base:
            return None
        if not base.startswith(("http://", "https://")):
            raise ValueError("api_base must be an http(s):// URL")
        return base

    def masked_key(self) -> str:
        """Render the key for display: enough to recognise it, never usable."""
        key = self.api_key.get_secret_value()
        return "****" if len(key) <= 8 else f"****{key[-4:]}"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "vendor": self.vendor,
            "api_base": self.api_base,
            "model": self.model,
            "fallback_model": self.fallback_model,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "is_multimodal": self.is_multimodal,
            "enabled": self.enabled,
            "key_configured": bool(self.api_key.get_secret_value()),
            "key_masked": self.masked_key(),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["api_key"] = self.api_key.get_secret_value()
        return data


class LLMProviderRegistry:
    """Persistent registry of user-configured providers + the default choice.

    JSON schema (``llm_providers.json``)::

        {
          "version": 1,
          "default_provider": "my-proxy",
          "providers": [
            {
              "name": "my-proxy",
              "kind": "openai",
              "api_base": "https://api.example.com/v1",
              "api_key": "sk-...",
              "model": "gpt-4o"
            }
          ]
        }
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._snapshot_data: dict[str, Any] | None = None

    def snapshot(self) -> LLMProviderRegistry:
        """Freeze one atomic registry revision for a task's endpoint preflight."""
        registry = LLMProviderRegistry(self.path)
        registry._snapshot_data = self._read()
        return registry

    @contextmanager
    def _locked(self):
        # filelock 3.32.3 (Unlicense), https://py-filelock.readthedocs.io/:
        # use its maintained Windows/POSIX process lock for the whole transaction.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock", mode=0o600):
            yield

    # ------------------------------------------------------------------ file
    @property
    def path(self) -> Path:
        return self._path if self._path is not None else get_providers_file()

    def _read(self) -> dict[str, Any]:
        if self._snapshot_data is not None:
            return deepcopy(self._snapshot_data)
        path = self.path
        if not path.exists():
            return {"version": 1, "default_provider": None, "providers": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"Could not read LLM provider registry {path}: {exc}")
            raise
        if not isinstance(data, dict):
            raise ValueError(f"LLM provider registry {path} must contain a JSON object")
        data.setdefault("providers", [])
        data.setdefault("default_provider", None)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp is unique and owner-only on POSIX. Windows also needs a
        # protected DACL; inherited directory permissions are insufficient.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                user_sid = _protect_registry_file(tmp)
                stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(path)
            _verify_registry_file_permissions(path, user_sid)
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ read
    def list(self) -> list[LLMProvider]:
        providers: list[LLMProvider] = []
        for raw in self._read().get("providers", []):
            try:
                providers.append(LLMProvider.model_validate(raw))
            except Exception:  # Invalid storage must not echo a plaintext key in validation errors.
                name = raw.get("name") if isinstance(raw, dict) else None
                logger.warning(f"Skipping malformed LLM provider entry {name!r}")
        return providers

    def get(self, name: str) -> LLMProvider | None:
        wanted = (name or "").strip().lower()
        for provider in self.list():
            if provider.name == wanted:
                return provider
        return None

    @property
    def default_provider_name(self) -> str | None:
        name = self._read().get("default_provider")
        return str(name).strip().lower() if name else None

    def default_provider(self) -> LLMProvider | None:
        name = self.default_provider_name
        return self.get(name) if name else None

    def resolve(self, name: str) -> LLMProvider | None:
        """Return the named entry, or the default entry when ``name`` is a
        default alias (``default``/``auto``/empty)."""
        wanted = (name or "").strip().lower()
        if wanted in ("", "default", "auto"):
            return self.default_provider()
        return self.get(wanted)

    def names(self) -> list[str]:
        return [p.name for p in self.list()]

    # ----------------------------------------------------------------- write
    def add(
        self,
        *,
        name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        kind: str = "openai",
        vendor: Literal["minimax"] | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        is_multimodal: bool = True,
        make_default: bool = False,
        replace: bool = True,
        enabled: bool | None = None,
        clear_api_key: bool = False,
    ) -> LLMProvider:
        """Register (or update) a provider. Secrets are stored only here."""
        with self._locked():
            return self._add_unlocked(
                name=name,
                api_key=api_key,
                api_base=api_base,
                model=model,
                fallback_model=fallback_model,
                kind=kind,
                vendor=vendor,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                is_multimodal=is_multimodal,
                make_default=make_default,
                replace=replace,
                enabled=enabled,
                clear_api_key=clear_api_key,
            )

    def _add_unlocked(self, **kwargs: Any) -> LLMProvider:
        name = kwargs["name"]
        data = self._read()
        providers = data.get("providers", [])
        existing_idx = next(
            (
                i
                for i, raw in enumerate(providers)
                if str(raw.get("name", "")).lower() == name.strip().lower()
            ),
            None,
        )
        existing = providers[existing_idx] if existing_idx is not None else {}
        api_key = (kwargs["api_key"] or "").strip()
        if kwargs["clear_api_key"]:
            if api_key:
                raise ValueError("api_key and clear_api_key are mutually exclusive")
            if existing_idx is None:
                raise ValueError("api_key is required for a new provider")
        elif not api_key:
            api_key = str(existing.get("api_key") or "")
            if not api_key:
                raise ValueError("api_key is required for a new provider")
        entry = LLMProvider(
            name=name,
            kind=kwargs["kind"],
            vendor=kwargs["vendor"],
            api_base=kwargs["api_base"],
            api_key=SecretStr(api_key),
            model=(kwargs["model"] or "").strip() or None,
            fallback_model=(kwargs["fallback_model"] or "").strip() or None,
            temperature=kwargs["temperature"],
            timeout_seconds=kwargs["timeout_seconds"],
            is_multimodal=kwargs["is_multimodal"],
            enabled=kwargs["enabled"]
            if kwargs["enabled"] is not None
            else existing.get("enabled", True),
        )
        if existing_idx is None:
            providers.append(entry.to_storage_dict())
        elif kwargs["replace"]:
            providers[existing_idx] = entry.to_storage_dict()
        else:
            raise ValueError(f"LLM provider '{entry.name}' already exists")
        data["providers"] = providers
        if kwargs["make_default"] or data.get("default_provider") in (None, ""):
            data["default_provider"] = entry.name
        self._write(data)
        logger.info(f"LLM provider '{entry.name}' saved to {self.path}")
        return entry

    def remove(self, name: str) -> bool:
        with self._locked():
            return self._remove_unlocked(name)

    def _remove_unlocked(self, name: str) -> bool:
        wanted = (name or "").strip().lower()
        data = self._read()
        providers = data.get("providers", [])
        remaining = [raw for raw in providers if str(raw.get("name", "")).lower() != wanted]
        if len(remaining) == len(providers):
            return False
        data["providers"] = remaining
        if str(data.get("default_provider") or "").lower() == wanted:
            data["default_provider"] = remaining[0].get("name") if remaining else None
        self._write(data)
        logger.info(f"LLM provider '{wanted}' removed from {self.path}")
        return True

    def set_default(self, name: str) -> LLMProvider:
        with self._locked():
            return self._set_default_unlocked(name)

    def _set_default_unlocked(self, name: str) -> LLMProvider:
        wanted = (name or "").strip().lower()
        provider = self.get(wanted)
        if provider is None:
            raise ValueError(f"unknown LLM provider '{name}'; known: {self.names() or 'none'}")
        data = self._read()
        data["default_provider"] = wanted
        self._write(data)
        logger.info(f"LLM default provider set to '{wanted}'")
        return provider


def get_providers_file() -> Path:
    """Path of the provider registry (never inside the git checkout)."""
    return get_app_dir() / PROVIDERS_FILENAME


#: Process-wide registry instance. Reads hit the file each call, so CLI/API
#: changes are visible to a running server without a restart.
provider_registry = LLMProviderRegistry()


def active_default_provider(registry: LLMProviderRegistry | None = None) -> LLMProvider | None:
    """The registry's default provider when it can actually serve traffic.

    Disabled entries count as unset: ``set_default`` can name an entry the
    operator later switched off, and nothing should silently route to a dead
    endpoint.
    """
    entry = (registry or provider_registry).default_provider()
    if entry is None or not entry.enabled:
        return None
    return entry


class ProviderResolution(BaseModel):
    """Outcome of mapping a configured ``provider`` value onto the registry."""

    provider: Any = Field(description="Wire provider name passed to ModelProvider.from_string")
    entry: LLMProvider | None = Field(default=None, description="Matching registry entry, if any")
    source: str = Field(
        default="builtin",
        description="How the entry was chosen: explicit | alias | default | fallback | builtin",
    )


def resolve_provider_for_model(
    provider_value: Any,
    *,
    builtin_has_key: bool | None = None,
    registry: LLMProviderRegistry | None = None,
) -> ProviderResolution:
    """Map a configured ``provider`` value onto the registry.

    ``source`` is one of:

    * ``explicit`` — ``provider`` named a registered provider;
    * ``alias``    — ``provider`` was ``default``/``auto``/empty → default entry;
    * ``fallback`` — a built-in provider without credentials was replaced by the
      default entry (the point of "allow setting one provider as default");
    * ``builtin``  — untouched built-in provider, ``entry`` is ``None``.

    ``builtin_has_key`` tells us whether the *built-in* provider configured for
    this model already has credentials. It only selects the ``source`` label
    (``fallback`` = the built-in had no key, ``default`` = the default provider
    won on precedence); either way an enabled registry default provider takes
    the call. ``None`` means "key status unknown" and is treated as ``default``.
    """
    raw = "" if provider_value is None else str(provider_value).strip().lower()
    registry = registry or provider_registry
    entry = registry.resolve(raw)
    if entry is not None:
        if not entry.enabled:
            if raw not in ("", "default", "auto"):
                raise ValueError(f"LLM provider '{raw}' is disabled")
            entry = None
    if entry is not None:
        if not entry.api_key.get_secret_value():
            raise ValueError(f"LLM provider '{entry.name}' has no API key")
        source = "alias" if raw in ("", "default", "auto") else "explicit"
        return ProviderResolution(provider=entry.kind, entry=entry, source=source)

    if raw in KEY_REQUIRED_BUILTINS:
        default = active_default_provider(registry)
        if default is not None:
            if not default.api_key.get_secret_value():
                raise ValueError(f"LLM provider '{default.name}' has no API key")
            # A registered default provider is *the* LLM of the product: it
            # takes every key-carrying built-in, with or without its own key.
            missing_key = isinstance(builtin_has_key, bool) and not builtin_has_key
            if missing_key:
                logger.info(
                    f"Provider '{raw}' has no API key configured; using default "
                    f"LLM provider '{default.name}' instead."
                )
            else:
                logger.debug(
                    f"Default LLM provider '{default.name}' takes precedence over built-in '{raw}'."
                )
            return ProviderResolution(
                provider=default.kind,
                entry=default,
                source="fallback" if missing_key else "default",
            )

    if raw not in RESERVED_NAMES:
        raise ValueError(f"Unknown LLM provider '{raw}'")
    if raw in ("", "default", "auto"):
        raise ValueError("No enabled default LLM provider is configured")
    return ProviderResolution(provider=provider_value, entry=None, source="builtin")


def builtin_provider_has_key(provider_value: Any) -> bool | None:
    """Whether the configured built-in provider already has credentials.

    Returns ``None`` for names that are not key-carrying built-ins (local
    ollama/vllm endpoints, registry names, aliases), which disables the
    default-provider reporting for them.
    """
    raw = "" if provider_value is None else str(provider_value).strip().lower()
    if raw not in KEY_REQUIRED_BUILTINS:
        return None
    try:
        from artemis.config import settings
        from artemis.config.settings import is_placeholder_key

        if settings.get_api_key(raw) is not None:
            return True
        # ``settings`` snapshots the environment at import time; a key exported
        # later (or written by ``artemis init``) must still count.
        for env_name in _BUILTIN_ENV_KEYS.get(raw, ()):
            val = os.environ.get(env_name)
            if val and val.strip() and not is_placeholder_key(val):
                return True
        return False
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def provider_credentials_available(provider_value: Any) -> bool:
    """Whether ``provider_value`` can actually authenticate a request.

    True when the name resolves to a registry entry (explicit or default alias),
    when it is a key-carrying built-in that has a key *or* an enabled registry
    default provider, or when it needs no key at all (local endpoints, Vertex AI
    ADC — those are validated on their own path).

    This is the single gate used by config validation (``LLM.validate_provider``)
    and the credentials readiness probe, so a registered default provider
    satisfies every "requires ..._API_KEY in .env" check without a built-in key.
    """
    raw = "" if provider_value is None else str(provider_value).strip().lower()
    entry = provider_registry.resolve(raw)
    if entry is not None:
        return entry.enabled and bool(entry.api_key.get_secret_value())
    if raw in KEY_REQUIRED_BUILTINS:
        if active_default_provider() is not None:
            return True
        return builtin_provider_has_key(raw) is True
    return raw in RESERVED_NAMES and raw not in ("default", "auto", "")


class ResolvedModelConfig(BaseModel):
    """The effective provider/model pair after registry resolution.

    Shared by the runtime model factory and the admin console's "active model
    configuration" display so the two can never disagree.
    """

    provider: Any = Field(description="Wire provider name passed to ModelProvider.from_string")
    display_provider: str = Field(description="Human label: registry entry name or built-in name")
    model: str
    api_key: str | None = Field(default=None, repr=False, exclude=True)
    api_base: str | None = None
    vendor: Literal["minimax"] | None = None
    registry_name: str | None = None
    source: str = "builtin"
    entry: LLMProvider | None = None


def resolve_model_config(
    provider_value: Any,
    model_value: Any = None,
    *,
    use_fallback: bool = False,
    builtin_has_key: bool | None = None,
    registry: LLMProviderRegistry | None = None,
) -> ResolvedModelConfig:
    """Resolve a configured provider/model pair onto an effective one.

    ``use_fallback`` mirrors the runtime fallback path: the entry's
    ``fallback_model`` wins over ``model`` (exactly like artemis.jsonc's
    ``default.fallback`` block).
    """
    raw_provider = "" if provider_value is None else str(provider_value).strip().lower()
    raw_model = "" if model_value is None else str(model_value).strip()
    model_is_alias = raw_model.lower() in ("", "default", "auto")

    resolution = resolve_provider_for_model(
        provider_value,
        builtin_has_key=(
            builtin_provider_has_key(provider_value) if builtin_has_key is None else builtin_has_key
        ),
        registry=registry,
    )
    entry = resolution.entry
    model_val = raw_model
    api_key = None
    api_base = None
    if entry is not None:
        api_key = entry.api_key.get_secret_value()
        api_base = entry.api_base
        if model_is_alias or resolution.source in ("fallback", "default"):
            # The entry carries its own model pair (default + fallback),
            # mirroring artemis.jsonc's default/fallback block.
            preferred = entry.fallback_model if use_fallback else entry.model
            model_val = preferred or entry.model or ""
            if model_val.lower() in ("", "default", "auto"):
                raise ValueError(f"LLM provider '{entry.name}' has no default model")
    if not model_val:
        model_val = "gemini-2.5-flash"

    return ResolvedModelConfig(
        provider=resolution.provider,
        display_provider=entry.name if entry is not None else (raw_provider or "google"),
        model=model_val,
        api_key=api_key,
        api_base=api_base,
        vendor=entry.vendor if entry is not None else None,
        registry_name=entry.name if entry is not None else None,
        source=resolution.source,
        entry=entry,
    )


# ---------------------------------------------------------------------------
# Model auto-discovery (OpenAI-compatible `GET /models`)
# Mirrors pi-web's model discovery: build the protocol-correct list URL, call it
# with the right auth header, and accept the handful of shapes upstreams return.
# ---------------------------------------------------------------------------

MODELS_LIST_TIMEOUT_SECONDS = 20.0

#: Response keys that may carry the model list.
_MODEL_LIST_KEYS = ("data", "models", "results", "items")


class DiscoveredModel(BaseModel):
    """One entry of an upstream model list."""

    id: str
    name: str | None = None


def build_models_list_url(api_base: str, *, kind: str = "openai") -> str:
    """Return the model-list endpoint for an OpenAI-compatible base URL."""
    base = (api_base or "").strip()
    if not base:
        raise ValueError("api_base is required to discover models")
    if not base.startswith(("http://", "https://")):
        raise ValueError("api_base must be an http(s):// URL")
    base = base.rstrip("/")
    if kind not in (*SUPPORTED_KINDS, "google"):
        raise ValueError(f"unsupported provider kind {kind!r}")
    if re.search(r"/models$", base, re.IGNORECASE):
        return base
    path = base
    if kind == "google" and not re.search(r"/v\d+[a-z]*$", path, re.IGNORECASE):
        path += "/v1beta"
    if kind == "anthropic" and not path.endswith("/v1"):
        path += "/v1"
    return f"{path}/models"


def _model_from_value(value: Any) -> DiscoveredModel | None:
    if isinstance(value, str):
        value = value.strip()
        return DiscoveredModel(id=value) if value else None
    if not isinstance(value, dict):
        return None
    raw_id = None
    for key in ("id", "model", "name"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            raw_id = candidate.strip()
            break
    if not raw_id:
        return None
    model_id = raw_id[len("models/") :] if raw_id.startswith("models/") else raw_id
    if not model_id:
        return None
    name = None
    for key in ("display_name", "displayName"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
    return DiscoveredModel(id=model_id, name=name if name and name != model_id else None)


def parse_discovered_models(payload: Any) -> list[DiscoveredModel]:
    """Normalise an upstream model list into de-duplicated, sorted entries."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = []
        for key in _MODEL_LIST_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
            if isinstance(candidate, dict):
                items = list(candidate.values())
                break
    else:
        items = []

    seen: set[str] = set()
    models: list[DiscoveredModel] = []
    for item in items:
        model = _model_from_value(item)
        if model is None or model.id in seen:
            continue
        seen.add(model.id)
        models.append(model)
    return sorted(models, key=lambda m: (m.name or m.id).lower())


async def discover_models(
    api_base: str,
    api_key: str | None = None,
    *,
    kind: str = "openai",
    timeout: float = MODELS_LIST_TIMEOUT_SECONDS,
) -> tuple[list[DiscoveredModel], str]:
    """Fetch the model list an OpenAI-compatible endpoint exposes.

    Returns ``(models, endpoint)``. Raises ``RuntimeError`` with an
    operator-readable message on transport or upstream failures.
    """
    import httpx

    endpoint = build_models_list_url(api_base, kind=kind)
    headers = {"Accept": "application/json"}
    if kind == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    models: dict[str, DiscoveredModel] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in range(20):
                kwargs: dict[str, Any] = {"headers": headers}
                if cursor:
                    kwargs["params"] = {"after_id": cursor}
                response = await client.get(endpoint, **kwargs)
                if response.status_code >= 400:
                    detail = response.text.strip()[:300] or response.reason_phrase
                    raise RuntimeError(f"Upstream returned HTTP {response.status_code}: {detail}")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("Upstream model list was not valid JSON") from exc
                for model in parse_discovered_models(payload):
                    models[model.id] = model
                if (
                    kind != "anthropic"
                    or not isinstance(payload, dict)
                    or not payload.get("has_more")
                ):
                    break
                next_cursor = payload.get("last_id")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    raise RuntimeError("Invalid or repeated upstream models cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise RuntimeError("Upstream models pagination exceeded 20 pages")
    except RuntimeError:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise RuntimeError(f"Could not reach {endpoint}: {exc}") from exc

    if not models:
        raise RuntimeError("No models found in the upstream response")
    return sorted(models.values(), key=lambda m: (m.name or m.id).lower()), endpoint
