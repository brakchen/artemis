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

"""Tests for the user-configured LLM provider registry and its resolution rules."""

from pathlib import Path

import pytest

from artemis.llm import providers as providers_module
from artemis.llm.providers import (
    LLMProvider,
    LLMProviderRegistry,
    resolve_provider_for_model,
)


@pytest.fixture()
def registry(tmp_path: Path) -> LLMProviderRegistry:
    return LLMProviderRegistry(path=tmp_path / "llm_providers.json")


@pytest.fixture()
def active_registry(registry: LLMProviderRegistry, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(providers_module, "provider_registry", registry)
    return registry


def test_add_and_list_roundtrip(registry: LLMProviderRegistry) -> None:
    registry.add(
        name="my-proxy",
        api_key="sk-secret-123456",
        api_base="https://api.example.com/v1/",
        model="gpt-4o",
    )
    entries = registry.list()
    assert [p.name for p in entries] == ["my-proxy"]
    entry = entries[0]
    assert entry.api_base == "https://api.example.com/v1"  # trailing slash normalised
    assert entry.api_key.get_secret_value() == "sk-secret-123456"
    assert entry.model == "gpt-4o"
    # secrets never surface in the public view
    public = entry.to_public_dict()
    assert "sk-secret" not in str(public)
    assert public["key_masked"] == "****3456"


def test_first_provider_becomes_default(registry: LLMProviderRegistry) -> None:
    registry.add(name="alpha", api_key="k1", api_base="https://a.example/v1")
    assert registry.default_provider_name == "alpha"
    registry.add(name="beta", api_key="k2", api_base="https://b.example/v1", make_default=True)
    assert registry.default_provider_name == "beta"


def test_set_default_and_remove_reassigns_default(registry: LLMProviderRegistry) -> None:
    registry.add(name="alpha", api_key="k1", api_base="https://a.example/v1")
    registry.add(name="beta", api_key="k2", api_base="https://b.example/v1")
    registry.set_default("alpha")
    assert registry.default_provider().name == "alpha"

    registry.remove("alpha")
    assert registry.default_provider_name == "beta"

    with pytest.raises(ValueError):
        registry.set_default("nope")


def test_rejects_reserved_and_bad_names(registry: LLMProviderRegistry) -> None:
    with pytest.raises(ValueError):
        registry.add(name="openai", api_key="k", api_base="https://a.example/v1")
    with pytest.raises(ValueError):
        registry.add(name="My Proxy!", api_key="k", api_base="https://a.example/v1")
    with pytest.raises(ValueError):
        registry.add(name="bad-kind", api_key="k", api_base="https://a.example/v1", kind="grpc")
    with pytest.raises(ValueError):
        registry.add(name="bad-url", api_key="k", api_base="ftp://a.example/v1")


def test_resolve_explicit_alias_and_default(active_registry: LLMProviderRegistry) -> None:
    active_registry.add(
        name="my-proxy", api_key="sk-1", api_base="https://a.example/v1", model="gpt-4o"
    )
    active_registry.add(
        name="other", api_key="sk-2", api_base="https://b.example/v1", model="qwen-max"
    )
    active_registry.set_default("other")

    explicit = resolve_provider_for_model("my-proxy", builtin_has_key=None)
    assert explicit.source == "explicit"
    assert explicit.entry is not None and explicit.entry.name == "my-proxy"
    assert explicit.provider == "openai"

    for alias in ("default", "auto", "", None):
        resolution = resolve_provider_for_model(alias, builtin_has_key=None)
        assert resolution.source == "alias"
        assert resolution.entry is not None and resolution.entry.name == "other"


def test_fallback_to_default_when_builtin_has_no_key(active_registry: LLMProviderRegistry) -> None:
    active_registry.add(
        name="my-proxy", api_key="sk-1", api_base="https://a.example/v1", model="gpt-4o"
    )
    active_registry.set_default("my-proxy")

    resolution = resolve_provider_for_model("google", builtin_has_key=False)
    assert resolution.source == "fallback"
    assert resolution.entry is not None and resolution.entry.name == "my-proxy"

    # A configured built-in keeps its own provider.
    assert resolve_provider_for_model("google", builtin_has_key=True).source == "builtin"
    # Local endpoints need no key, so they are never swapped out.
    assert resolve_provider_for_model("ollama", builtin_has_key=None).source == "builtin"


def test_registry_survives_reload(registry: LLMProviderRegistry) -> None:
    registry.add(name="p1", api_key="sk-x", api_base="https://a.example/v1", model="m1")
    registry.set_default("p1")
    fresh = LLMProviderRegistry(path=registry.path)
    assert [p.name for p in fresh.list()] == ["p1"]
    assert fresh.default_provider_name == "p1"
    assert fresh.get("p1").api_key.get_secret_value() == "sk-x"


def test_provider_model_validation() -> None:
    entry = LLMProvider(name="X-Y", api_key="sk-1", api_base="https://a.example/v1")
    assert entry.name == "x-y"
    assert entry.kind == "openai"


# ---------------------------------------------------------------------------
# Model auto-discovery (OpenAI-compatible GET /models)
# ---------------------------------------------------------------------------


def test_build_models_list_url() -> None:
    from artemis.llm.providers import build_models_list_url

    assert build_models_list_url("https://api.example.com/v1") == "https://api.example.com/v1/models"
    assert build_models_list_url("https://api.example.com/v1/") == "https://api.example.com/v1/models"
    assert build_models_list_url("https://api.example.com/v1/models") == "https://api.example.com/v1/models"
    assert build_models_list_url("http://localhost:11434/v1") == "http://localhost:11434/v1/models"

    with pytest.raises(ValueError):
        build_models_list_url("")
    with pytest.raises(ValueError):
        build_models_list_url("api.example.com/v1")


def test_parse_discovered_models_shapes() -> None:
    from artemis.llm.providers import parse_discovered_models

    # bare array, string entries
    assert [m.id for m in parse_discovered_models(["gpt-4o", "gpt-4o-mini"])] == [
        "gpt-4o",
        "gpt-4o-mini",
    ]
    # OpenAI-style {data: [{id}]} + "models/" prefix stripping + de-duplication
    payload = {"data": [{"id": "models/gemini-2.0"}, {"id": "gemini-2.0"}, {"id": "claude-x"}]}
    assert [m.id for m in parse_discovered_models(payload)] == ["claude-x", "gemini-2.0"]
    # other upstream shapes
    assert [m.id for m in parse_discovered_models({"models": [{"model": "a"}]})] == ["a"]
    assert [m.id for m in parse_discovered_models({"results": [{"name": "b"}]})] == ["b"]
    assert [m.id for m in parse_discovered_models({"items": [{"id": "c"}]})] == ["c"]
    # display name is kept when it differs from the id
    (m,) = parse_discovered_models([{"id": "m1", "display_name": "GPT-4o"}])
    assert m.name == "GPT-4o"
    # junk is ignored
    assert parse_discovered_models({"data": [{"foo": 1}, None, 3]}) == []
    assert parse_discovered_models(None) == []


def test_provider_stores_fallback_model(registry: LLMProviderRegistry) -> None:
    registry.add(
        name="my-proxy",
        api_key="sk-1",
        api_base="https://a.example/v1",
        model="gpt-4o",
        fallback_model="gpt-4o-mini",
    )
    entry = registry.get("my-proxy")
    assert entry is not None
    assert entry.model == "gpt-4o"
    assert entry.fallback_model == "gpt-4o-mini"
    assert entry.to_public_dict()["fallback_model"] == "gpt-4o-mini"
