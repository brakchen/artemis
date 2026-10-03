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

"""Repository-wide pytest fixtures and classification helpers.

The default test paths contain only deterministic tests.  Tests under the
integration and end-to-end trees remain directly runnable, and receive stable
markers here so callers can select them without relying on filename patterns.
"""

from pathlib import Path

import pytest

from artemis.drivers.mock.mock_driver import MockDeviceDriver


@pytest.fixture
def mock_driver():
    """Provide an isolated mock mobile driver."""
    return MockDeviceDriver(device_id="fixture-mock-device", width=1080, height=2400)


@pytest.fixture(autouse=True)
def isolated_llm_provider_registry(tmp_path_factory, monkeypatch):
    """Run every test against a throwaway LLM provider registry.

    The real registry lives in the user's app dir, and it holds a *default*
    provider — which the runtime (and the credentials probe) honour for every
    node. Leaking it would make tests depend on the developer's machine, so each
    test gets an empty registry plus a deterministic default entry: that is the
    credential shape an installation without any built-in API key has, and the
    one unit tests must be able to run under (``make test`` promises tests that
    need no credentials).
    """
    from artemis.llm import providers as providers_module

    registry = providers_module.LLMProviderRegistry(
        path=tmp_path_factory.mktemp("llm-providers") / "llm_providers.json"
    )
    # The first registered provider becomes the default, so nothing else to do.
    registry.add(
        name="unit-test-llm",
        api_key="sk-unit-test",
        api_base="https://unit.test.invalid/v1",
        model="unit-test-model",
    )
    monkeypatch.setattr(providers_module, "provider_registry", registry)
    return registry


@pytest.fixture
def no_default_llm_provider(tmp_path: Path, monkeypatch):
    """Opt out of the fixture default: an empty registry, no credentials at all."""
    from artemis.llm import providers as providers_module

    registry = providers_module.LLMProviderRegistry(path=tmp_path / "llm_providers.json")
    monkeypatch.setattr(providers_module, "provider_registry", registry)
    return registry


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Attach test-layer markers according to the owning test directory."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "e2e" in parts:
            item.add_marker(pytest.mark.e2e)
