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

from __future__ import annotations

import asyncio
import logging
import json
from io import StringIO
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
from app.config import settings

logger = logging.getLogger("artemis.docker")

try:
    import docker
    from docker.errors import DockerException, NotFound

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


class DockerManagerService:
    """Manages creation, monitoring, and deletion of ephemeral Artemis session containers via Docker Socket."""

    def __init__(self):
        self._owned_exports: dict[Path, Path] = {}
        self._container_exports: dict[str, Path] = {}
        self._client: docker.DockerClient | None = None
        if DOCKER_AVAILABLE:
            try:
                self._client = docker.DockerClient(base_url=settings.DOCKER_SOCKET_PATH)
                logger.info(
                    f"[Docker Service] Connected to Docker socket at {settings.DOCKER_SOCKET_PATH}"
                )
                self._ensure_network()
            except Exception as e:
                logger.warning(
                    f"[Docker Service] Could not connect to Docker socket: {e}. Running in simulation mode."
                )
                self._client = None
        else:
            logger.warning("[Docker Service] Docker SDK not available; running in simulation mode.")

    def _ensure_network(self) -> None:
        """Ensure the shared user-defined bridge network exists."""
        if not self._client:
            return
        try:
            self._client.networks.get(settings.DOCKER_NETWORK)
        except NotFound:
            logger.info(f"[Docker Service] Creating network {settings.DOCKER_NETWORK}")
            self._client.networks.create(settings.DOCKER_NETWORK, driver="bridge")
        except Exception as e:
            logger.warning(f"[Docker Service] Network check warning: {e}")

    def _container_configuration(self, profile: str) -> tuple[dict, dict, dict, Path]:
        """Export only the selected profile and credentials after pure preflight.

        Docker resolves bind sources on its host, which may differ from the
        manager's filesystem. Both roots must be configured explicitly.
        """
        from artemis.config.agent import AgentGlobalConfig
        from artemis.config.paths import get_main_config_path
        from artemis.config.constants import LLM_CONFIG_FILENAME, AGENT_CONFIG_FILENAME
        from third_party.mobile_use.utils.file import load_jsonc
        from artemis.config.task import prepare_task_submission
        from artemis.interfaces.cli.commands.init import _protected_write, _merge_env
        from artemis.llm.providers import _BUILTIN_ENV_KEYS

        # Capture inputs before building. A post-build baseline would silently
        # accept an edit between parsing and snapshot publication.
        source_paths = {
            get_main_config_path(LLM_CONFIG_FILENAME),
            get_main_config_path(AGENT_CONFIG_FILENAME),
        }
        sources = {path: path.read_bytes() for path in source_paths}
        agent_path = get_main_config_path(AGENT_CONFIG_FILENAME)
        agent_raw = load_jsonc(StringIO(sources[agent_path].decode("utf-8")))
        agent_payload = agent_raw.get("agent", agent_raw)
        if "agent" not in agent_raw and ("default" in agent_raw or "nodes" in agent_raw):
            # A unified role-only file is also accepted by the legacy Agent
            # loader, whose extra=allow would otherwise copy unused presets
            # into the nested Agent block. Only its actual runtime fields apply.
            agent_payload = {
                key: value
                for key, value in agent_raw.items()
                if key in AgentGlobalConfig.model_fields
            }
        agent_config = AgentGlobalConfig.model_validate(agent_payload)
        prepared = prepare_task_submission(profile)
        self._check_configuration_sources(sources)
        endpoints = list(prepared._llm_endpoint_snapshot.values())
        # Legacy configuration views may pin absent keys; a runnable container
        # must have every credential required by its selected endpoints.
        from artemis.llm.resolution import _require_credentials

        for endpoint in endpoints:
            _require_credentials(endpoint)
        root = Path(settings.ARTEMIS_EXPORT_ROOT).resolve()
        host_value = settings.ARTEMIS_EXPORT_HOST_ROOT
        host_type = PureWindowsPath if PureWindowsPath(host_value).drive else PurePosixPath
        host_root = host_type(host_value)
        if not host_value or not host_root.is_absolute() or ".." in host_root.parts:
            raise ValueError(
                "Configure an absolute ARTEMIS_EXPORT_HOST_ROOT mapped to ARTEMIS_EXPORT_ROOT"
            )
        registry = prepared._llm_provider_registry
        names = {endpoint.registry_name for endpoint in endpoints if endpoint.registry_name}
        entries = [registry.get(name) for name in sorted(names)]
        if any(entry is None for entry in entries):
            raise ValueError("Selected provider registry changed during container preflight")
        default_name = registry.default_provider_name
        registry_data = {
            "version": 1,
            "default_provider": default_name if default_name in names else None,
            "providers": [entry.to_storage_dict() for entry in entries],
        }
        main = prepared.llm_config.model_dump(mode="json", exclude_unset=True)
        main["agent"] = agent_config.model_dump(mode="json", exclude_unset=True)
        legacy_env = {}
        for endpoint in endpoints:
            if endpoint.registry_name:
                continue
            keys = _BUILTIN_ENV_KEYS.get(endpoint.provider.value, ())
            if endpoint.provider.value in {"custom", "ollama", "vllm"}:
                # Canonical resolution uses OPENAI_API_KEY for these selected
                # transports too; EMPTY is its no-auth fallback, not a secret.
                keys = ("OPENAI_API_KEY",) if endpoint.api_key != "EMPTY" else ()
            if endpoint.api_key and keys:
                legacy_env[keys[0]] = endpoint.api_key
            if (
                endpoint.provider.value in {"openai", "custom", "ollama", "vllm"}
                and endpoint.api_base
            ):
                legacy_env["OPENAI_BASE_URL"] = endpoint.api_base
            if endpoint.provider.value == "anthropic" and endpoint.api_base:
                legacy_env["ANTHROPIC_BASE_URL"] = endpoint.api_base
        # Independent Google Vision credentials are only used by legacy OCR.
        if not any(endpoint.vendor == "minimax" for endpoint in endpoints):
            from artemis.config import settings as core_settings

            ocr = core_settings.get_api_key("ocr")
            if ocr is not None:
                legacy_env["OCR_API_KEY"] = ocr.get_secret_value()
        files = {
            "artemis.jsonc": (json.dumps(main, ensure_ascii=False) + "\n").encode(),
            "llm_providers.json": (json.dumps(registry_data, ensure_ascii=False) + "\n").encode(),
            ".env": _merge_env("", legacy_env).encode(),
        }
        targets = {
            "artemis.jsonc": "/run/artemis/artemis.jsonc",
            "llm_providers.json": "/var/lib/artemis/llm_providers.json",
            ".env": "/var/lib/artemis/.env",
        }
        self._check_configuration_sources(sources)
        root.mkdir(parents=True, exist_ok=True)
        export = Path(tempfile.mkdtemp(prefix="session-", dir=root)).resolve()
        export.relative_to(root)
        self._owned_exports[export] = root
        host_export = host_root.joinpath(*export.relative_to(root).parts)
        volumes = {}
        try:
            for filename, content in files.items():
                path = export / filename
                if path.resolve().parent != export:
                    raise ValueError("Container configuration export escaped its session directory")
                _protected_write(path, content)
                volumes[str(host_export / filename)] = {"bind": targets[filename], "mode": "ro"}
        except Exception as error:
            self._cleanup_failed_start(export, error)
            raise
        return (
            {
                "ARTEMIS_ARTEMIS_JSONC": "/run/artemis/artemis.jsonc",
                "ARTEMIS_APP_DIR": "/var/lib/artemis",
                "ARTEMIS_CONFIG_READ_ONLY": "1",
            },
            volumes,
            sources,
            export,
        )

    def _cleanup_export(self, export: Path) -> None:
        """Delete only this service's generated files and their empty directory."""
        root = self._owned_exports.get(export)
        if root is None:
            raise ValueError("Configuration export is not owned by this service")
        if root.resolve() != root or export.resolve() != export or export.parent != root:
            raise ValueError("Configuration export cleanup escaped its owned directory")
        for filename in ("artemis.jsonc", "llm_providers.json", ".env"):
            (export / filename).unlink(missing_ok=True)
        if export.exists():
            export.rmdir()
        self._owned_exports.pop(export)

    def _release_container_export(self, container_id: str) -> None:
        export = self._container_exports.get(container_id)
        if export is not None:
            self._cleanup_export(export)
            self._container_exports.pop(container_id)

    def _cleanup_failed_start(self, export: Path, error: BaseException) -> None:
        try:
            self._cleanup_export(export)
        except Exception as cleanup_error:
            error.add_note(
                f"Owned configuration export cleanup failed: {type(cleanup_error).__name__}"
            )
            logger.warning(
                "Owned configuration export cleanup failed (%s)", type(cleanup_error).__name__
            )

    @staticmethod
    def _check_configuration_sources(sources: dict) -> None:
        if any(
            not path.is_file() or path.read_bytes() != content for path, content in sources.items()
        ):
            raise ValueError(
                "Model configuration changed during container preflight; retry the request"
            )

    async def start_artemis_container(
        self,
        session_id: str,
        cuttlefish_host: str,
        cuttlefish_adb_port: int,
        *,
        profile: str = "flash",
    ) -> dict:
        """Spawn a dedicated Artemis session container connected to the target Cuttlefish emulator."""
        config_environment, volumes, sources, export = self._container_configuration(profile)
        container_name = f"{settings.ARTEMIS_CONTAINER_PREFIX}-{session_id}"
        adb_target = f"{cuttlefish_host}:{cuttlefish_adb_port}"

        logger.info(
            f"[Docker Service] Launching container '{container_name}' targeting ADB device '{adb_target}'..."
        )

        try:
            self._check_configuration_sources(sources)
        except Exception as error:
            self._cleanup_failed_start(export, error)
            raise
        if not self._client:
            self._cleanup_export(export)
            # Simulated mode for environments without raw Docker socket access
            await asyncio.sleep(0.5)
            return {
                "container_id": f"mock-container-{session_id[:8]}",
                "container_name": container_name,
                "internal_ip": "172.20.0.5",
            }

        loop = asyncio.get_event_loop()

        created = []

        def _run_container():
            self._check_configuration_sources(sources)
            # Check if an existing container with same name exists and remove it
            try:
                old = self._client.containers.get(container_name)
                old.remove(force=True)
                self._release_container_export(old.id)
            except NotFound:
                pass

            # Assemble container environment
            env_vars = {
                "SESSION_ID": session_id,
                "ADB_DEVICE_SERIAL": adb_target,
                "PORT": "8080",
                "PYTHONUNBUFFERED": "1",
            }
            env_vars.update(config_environment)

            # Launch Artemis container
            container = self._client.containers.run(
                image=settings.ARTEMIS_IMAGE,
                name=container_name,
                detach=True,
                network=settings.DOCKER_NETWORK,
                extra_hosts={"host.docker.internal": "host-gateway"},
                environment=env_vars,
                volumes=volumes,
                labels={
                    "artemis.managed": "true",
                    "artemis.session_id": session_id,
                },
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
            )

            created.append(container)
            self._container_exports[container.id] = export

            # Reload to get network IP
            container.reload()
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            internal_ip = networks.get(settings.DOCKER_NETWORK, {}).get("IPAddress", "")

            # Execute adb connect inside the container to establish connection with Cuttlefish
            try:
                connect_cmd = f"adb connect {adb_target}"
                logger.info(f"[Docker Service] Executing '{connect_cmd}' in {container_name}...")
                container.exec_run(connect_cmd)
            except Exception as e:
                logger.warning(f"[Docker Service] adb connect command notice: {e}")

            return {
                "container_id": container.id,
                "container_name": container_name,
                "internal_ip": internal_ip,
            }

        from artemis.utils.video import drain_media_task

        worker = None
        try:
            worker = loop.run_in_executor(None, _run_container)
            result = await asyncio.shield(worker)
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError) and worker is not None:
                # Executor cancellation cannot stop Docker. Join the actual
                # operation before inspecting created IDs or deleting bind files.
                try:
                    await drain_media_task(worker)
                except asyncio.CancelledError:
                    logger.debug("Container start worker settled despite repeated cancellation")
                except Exception as worker_error:
                    error.add_note(
                        f"Container start worker failed after cancellation: {type(worker_error).__name__}"
                    )
                    logger.warning(
                        "Container start worker failed after cancellation (%s)",
                        type(worker_error).__name__,
                    )

            async def cleanup_start(original_error: BaseException) -> None:
                if created:
                    try:
                        await loop.run_in_executor(None, lambda: created[0].remove(force=True))
                    except Exception as removal_error:
                        # Keep bind files when the actual created container may
                        # still use them. Preserve the original start/cancel error.
                        original_error.add_note(
                            f"Created container removal failed; configuration export retained: {type(removal_error).__name__}"
                        )
                        logger.warning(
                            "Created container removal failed; configuration export retained (%s)",
                            type(removal_error).__name__,
                        )
                        return
                    self._container_exports.pop(created[0].id, None)
                self._cleanup_failed_start(export, original_error)

            cleanup = asyncio.create_task(cleanup_start(error))
            try:
                # The existing drain accepts Future/Task and joins even when
                # the caller is cancelled again during owned container removal.
                await drain_media_task(cleanup)
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    error.add_note(
                        "Owned container cleanup was cancelled; configuration export retained"
                    )
                    logger.warning(
                        "Owned container cleanup was cancelled; configuration export retained"
                    )
                else:
                    logger.debug("Owned container cleanup settled despite repeated cancellation")
            except Exception as cleanup_error:
                error.add_note(f"Owned container cleanup failed: {type(cleanup_error).__name__}")
                logger.warning("Owned container cleanup failed (%s)", type(cleanup_error).__name__)
            raise
        logger.info(f"[Docker Service] Container {container_name} successfully booted.")
        return result

    async def stop_artemis_container(self, session_id: str) -> None:
        """Stop and remove an Artemis session container."""
        container_name = f"{settings.ARTEMIS_CONTAINER_PREFIX}-{session_id}"
        logger.info(f"[Docker Service] Stopping and removing container '{container_name}'...")

        if not self._client:
            return

        loop = asyncio.get_event_loop()

        def _remove_container():
            try:
                container = self._client.containers.get(container_name)
                container.stop(timeout=5)
                container.remove(force=True)
                self._release_container_export(container.id)
                logger.info(f"[Docker Service] Container {container_name} removed.")
            except NotFound:
                logger.info(f"[Docker Service] Container {container_name} was already removed.")
            except Exception as e:
                logger.error(f"[Docker Service] Error removing container {container_name}: {e}")
                raise

        await loop.run_in_executor(None, _remove_container)


docker_manager = DockerManagerService()
