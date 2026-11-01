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

"""Interactive setup wizard for first-time ARTEMIS onboarding (artemis init)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from io import StringIO
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Annotated

import typer
from filelock import FileLock

import shutil
import subprocess

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from artemis.config.paths import get_env_file


def _protected_write(path: Path, content: bytes) -> None:
    """Publish one owner-only file; this is not a multi-file transaction."""
    from artemis.llm.providers import _protect_registry_file, _verify_registry_file_permissions

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            sid = _protect_registry_file(temporary)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _verify_registry_file_permissions(path, sid)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_env(content: str, updates: dict[str, str]) -> str:
    remaining = dict(updates)
    lines = []
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)=", line)
        if match and match[1] in updates:
            key = match[1]
            if key in remaining:
                lines.append(f"{key}={json.dumps(remaining.pop(key), ensure_ascii=False)}")
        else:
            lines.append(line)
    lines.extend(
        f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in remaining.items()
    )
    return "\n".join(lines) + "\n"


def _minimax_config(data: dict, name: str, model: str) -> dict:
    """Migrate active roles and explicit auxiliaries, preserving unrelated knobs."""
    from artemis.config.llm import AGENT_ROLES, UTILITY_ROLES

    result = deepcopy(data)
    incompatible = {
        "thinking_level",
        "thinking_budget",
        "include_thoughts",
        "thinking_mode",
        "reasoning_effort",
    }

    def migrate(value):
        value = deepcopy(value) if isinstance(value, dict) else {}
        fallback = value.pop("fallback", {})
        for key in incompatible:
            value.pop(key, None)
        value.update(provider=name, model=model)
        if model.lower() == "minimax-m3.1-flash":
            value.update(thinking_mode="adaptive", reasoning_effort="medium")
        else:
            value["thinking_mode"] = "disabled"
        fallback = deepcopy(fallback) if isinstance(fallback, dict) else {}
        for key in incompatible:
            fallback.pop(key, None)
        fallback.update(provider=name, model=model)
        if model.lower() == "minimax-m3.1-flash":
            fallback.update(thinking_mode="adaptive", reasoning_effort="medium")
        else:
            fallback["thinking_mode"] = "disabled"
        value["fallback"] = fallback
        return value

    result["default"] = migrate(result.get("default", result.get("operator", {})))
    old_nodes = result.get("nodes", {})
    old_utils = result.pop("utils", {}) or {}
    result["nodes"] = {
        role: migrate(old_nodes.get(role, result.pop(role, old_utils.get(role, {}))))
        for role in (*AGENT_ROLES, *UTILITY_ROLES)
    }
    agent = result.setdefault("agent", {})
    for parent, key in (
        (agent.setdefault("flash", {}), "step_summarizer"),
        (agent.setdefault("memory", {}), "chunking"),
    ):
        auxiliary = parent.setdefault(key, {})
        for parameter in incompatible:
            auxiliary.pop(parameter, None)
        auxiliary["model"] = model
        if model.lower() == "minimax-m3.1-flash":
            auxiliary.update(thinking_mode="adaptive", reasoning_effort="medium")
        else:
            auxiliary["thinking_mode"] = "disabled"
    return result


def migrate_minimax(
    *,
    name: str,
    api_key: str | None,
    base_url: str | None,
    model: str,
    config_path: Path | None = None,
) -> tuple[Path, list[tuple[Path, Path]]]:
    """Validate an unpublished candidate, back up, then conditionally publish.

    Rollback restores only bytes still equal to this invocation's writes. A
    later user's edit is retained, with the protected backups available to them.
    """
    from artemis.config.agent import AgentGlobalConfig
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.config.paths import get_app_dir, get_main_config_path, is_source_checkout
    from artemis.config.task import prepare_selected_task_config
    from artemis.llm.providers import provider_registry
    from third_party.mobile_use.utils.file import load_jsonc

    source = config_path or get_main_config_path("llm-config.json")
    target = source
    if config_path is None and not is_source_checkout():
        # Wheel resources are templates. Only the user application copy is mutable.
        explicit = os.getenv("ARTEMIS_ARTEMIS_JSONC") or os.getenv("ARTEMIS_ARTEMIS_JSON")
        target = Path(explicit) if explicit else get_app_dir() / "artemis.jsonc"
    # Parse exactly the revision guarded below. Capturing a later baseline after
    # derivation would mistake an intervening user edit for the candidate input.
    source_before = source.read_bytes()
    target_before = (
        source_before
        if source.resolve() == target.resolve()
        else (target.read_bytes() if target.exists() else None)
    )
    data = load_jsonc(StringIO(source_before.decode("utf-8")))
    if "agent" not in data:
        # Independent legacy agent config remains a supported input to migration.
        from artemis.config.agent import load_agent_config

        data["agent"] = load_agent_config().model_dump(exclude_unset=True)
    _expand_default_into_nodes(data)  # Reject misspelled roles before migration can discard them.
    candidate = _minimax_config(data, name, model)
    typed = LLMConfig.model_validate(_expand_default_into_nodes(candidate))
    agent = AgentGlobalConfig.model_validate(candidate["agent"])
    env_path = get_env_file()
    env_before = env_path.read_bytes() if env_path.exists() else None
    env_after = _merge_env(
        (env_before or b"").decode("utf-8"), {"ARTEMIS_ARTEMIS_JSONC": str(target.resolve())}
    ).encode()
    target_after = (json.dumps(candidate, indent=2, ensure_ascii=False) + "\n").encode()
    backups: list[tuple[Path, Path]] = []
    writes: dict[Path, bytes] = {}
    originals: dict[Path, bytes | None] = {}
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", mode=0o600), provider_registry._locked():
        entry = provider_registry.candidate(
            name=name,
            kind="anthropic",
            vendor="minimax",
            api_base=base_url
            or (
                provider_registry.get(name).api_base
                if provider_registry.get(name)
                else "https://api.minimax.cn/anthropic"
            ),
            api_key=api_key,
            model=model,
            fallback_model=model,
            enabled=True,
        )
        from artemis.config.settings import is_placeholder_key

        if is_placeholder_key(entry.api_key):
            raise ValueError(
                "Enter a real MiniMax API key instead of an empty or placeholder value"
            )
        revision = provider_registry.with_candidate(entry, make_default=True)
        # Same endpoint/auxiliary validators as real tasks; never monkeypatch globals.
        prepare_selected_task_config(
            agent, SimpleNamespace(name="default", llm_config=typed), registry=revision
        )
        originals = {
            provider_registry.path: provider_registry.path.read_bytes()
            if provider_registry.path.exists()
            else None,
            target: target_before,
            env_path: env_before,
        }
        for path, before in {source: source_before, **originals}.items():
            current = path.read_bytes() if path.exists() else None
            if current != before:
                raise ValueError("Configuration changed during setup; retry with the latest files")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        for path, before in originals.items():
            if before is not None:
                backup = path.with_name(path.name + f".backup-{stamp}")
                _protected_write(backup, before)
                backups.append((path, backup))
        try:
            # Individual atomic replacements, with conditional rollback on failure.
            registry_after = (
                json.dumps(revision._read(), indent=2, ensure_ascii=False) + "\n"
            ).encode()
            for path, after in (
                (provider_registry.path, registry_after),
                (target, target_after),
                (env_path, env_after),
            ):
                current = path.read_bytes() if path.exists() else None
                if current != originals[path]:
                    raise ValueError(
                        "Configuration changed during setup; review backups before retrying"
                    )
                writes[path] = after
                _protected_write(path, after)
        except BaseException as error:
            setattr(error, "minimax_backups", backups)
            rollback_failed = []
            for path, after in reversed(list(writes.items())):
                try:
                    if path.exists() and path.read_bytes() == after:
                        before = originals[path]
                        if before is None:
                            path.unlink()
                        else:
                            _protected_write(path, before)
                except OSError:
                    rollback_failed.append(path)
            setattr(error, "minimax_rollback_failed", rollback_failed)
            raise
    os.environ["ARTEMIS_ARTEMIS_JSONC"] = str(target.resolve())
    return target, backups


def init_command(
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Use minimax for complete MiniMax role migration."),
    ] = None,
    name: Annotated[str, typer.Option("--name", help="MiniMax registry name.")] = "minimax",
    import_env: Annotated[
        bool,
        typer.Option(
            "--import-env",
            help="Explicitly import MINIMAX_API_KEY/MINIMAX_BASE_URL from this process.",
        ),
    ] = False,
    replace_key: Annotated[
        bool,
        typer.Option("--replace-key", help="Explicitly replace an existing key with --import-env."),
    ] = False,
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    model: Annotated[
        str, typer.Option("--model", help="MiniMax-M3.1-Flash (default) or MiniMax-M3.")
    ] = "MiniMax-M3.1-Flash",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Selected main config to migrate.")
    ] = None,
) -> None:
    """Interactive quickstart wizard to configure API keys and connect your device."""
    console = Console()
    console.print()
    console.print(
        Panel(
            "[bold cyan]☕ Welcome to Artemis Mobile Testing Framework[/bold cyan]\n"
            "[dim]Let's get you set up in 10 seconds![/dim]",
            expand=False,
        )
    )

    # 1. Select Provider
    console.print("\n[bold]Step 1: Choose Primary LLM Provider[/bold]")
    console.print("  [1] Google Gemini (Recommended - Default)")
    console.print("  [2] OpenAI (GPT-4o)")
    console.print("  [3] Anthropic (Claude 3.5 Sonnet)")
    console.print("  [4] OpenRouter")

    console.print("  [5] MiniMax (Anthropic Messages; migrate all task roles)")
    if provider is not None and provider != "minimax":
        console.print(
            "[red]--provider currently supports minimax; omit it for legacy choices.[/red]"
        )
        raise typer.Exit(code=1)
    choice = (
        "5"
        if provider == "minimax"
        else Prompt.ask("Select provider", choices=["1", "2", "3", "4", "5"], default="1")
    )
    if choice == "5":
        from artemis.llm.providers import provider_registry

        existing = provider_registry.get(name)
        key = os.getenv("MINIMAX_API_KEY") if import_env else None
        if replace_key and not import_env:
            console.print(
                "[red]--replace-key requires --import-env. Manual replacement uses the hidden prompt.[/red]"
            )
            raise typer.Exit(code=1)
        if existing and import_env and not replace_key:
            key = None
        if import_env and (existing is None or replace_key):
            if key is None:
                console.print(
                    "[red]MINIMAX_API_KEY is missing from this process. Set it explicitly or omit --import-env for the hidden prompt.[/red]"
                )
                raise typer.Exit(code=1)
            from artemis.config.settings import is_placeholder_key

            # Explicit import/replacement is a source selection, not an ordinary
            # blank edit. Reject it before the candidate's blank-key reuse rule.
            key = key.strip()
            if is_placeholder_key(key):
                console.print(
                    "[red]MINIMAX_API_KEY must contain a real, non-empty API key. No configuration was changed.[/red]"
                )
                raise typer.Exit(code=1)
        selected_base = base_url or (os.getenv("MINIMAX_BASE_URL") if import_env else None)
        if not key and existing is None:
            key = Prompt.ask("Enter MiniMax API key", password=True)
        elif not import_env:
            key = Prompt.ask(
                "Replacement MiniMax API key (blank keeps stored key)",
                password=True,
                default="",
                show_default=False,
            )
        try:
            target, backups = migrate_minimax(
                name=name, api_key=key, base_url=selected_base, model=model, config_path=config_path
            )
        except Exception as error:
            console.print(
                "[red]MiniMax setup did not complete. Check the selected config, protocol and key, and review protected backups before retrying.[/red]"
            )
            for original, backup in getattr(error, "minimax_backups", []):
                console.print(f"Protected backup: {backup} → restore to {original}", markup=False)
            for path in getattr(error, "minimax_rollback_failed", []):
                console.print(
                    f"Automatic rollback could not restore: {path}. Use the protected backup after reviewing current edits.",
                    markup=False,
                )
            console.print(
                "Rollback: compare the listed protected backups with current files before restoring; preserve later edits.",
                markup=False,
            )
            raise typer.Exit(code=1) from None
        console.print(f"MiniMax task-role configuration saved to {target}", markup=False)
        console.print(
            "Canonical local preflight passed. Catalog access, account permissions and localization quality still require verification.",
            markup=False,
        )
        for original, backup in backups:
            console.print(f"Protected backup: {backup} → restore to {original}", markup=False)
        console.print(
            "Rollback: stop new tasks, compare current files, then restore the listed protected backups together. Do not overwrite later edits.",
            markup=False,
        )
        return
    provider_map = {
        "1": ("GEMINI_API_KEY", "Google Gemini"),
        "2": ("OPENAI_API_KEY", "OpenAI"),
        "3": ("ANTHROPIC_API_KEY", "Anthropic"),
        "4": ("OPEN_ROUTER_API_KEY", "OpenRouter"),
    }
    env_key, provider_name = provider_map[choice]

    # 2. Enter API Key
    console.print(f"\n[bold]Step 2: Enter your {provider_name} API Key[/bold]")
    api_key = Prompt.ask(f"Enter {env_key}", password=True)
    while not api_key.strip():
        console.print("[bold red]API Key cannot be empty.[/bold red]")
        api_key = Prompt.ask(f"Enter {env_key}", password=True)

    # 3. Scan for Android Devices
    console.print("\n[bold]Step 3: Detecting Android Devices / Emulators...[/bold]")
    detected_devices = []
    adb_path = shutil.which("adb")
    if adb_path:
        try:
            res = subprocess.run(
                [adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [line_str.strip() for line_str in res.stdout.splitlines() if line_str.strip()]
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    serial = parts[0]
                    extra = " ".join(parts[2:])
                    detected_devices.append((serial, extra))
        except (OSError, subprocess.SubprocessError):
            # adb probe failed or timed out; continue setup with no devices.
            pass

    selected_serial = ""
    if detected_devices:
        console.print(
            f"[bold green]✔ Found {len(detected_devices)} connected device(s):[/bold green]"
        )
        for idx, (serial, extra) in enumerate(detected_devices, 1):
            console.print(f"  [{idx}] {serial} ({extra})")
        if len(detected_devices) == 1:
            selected_serial = detected_devices[0][0]
            console.print(f"[dim]Auto-selected device: {selected_serial}[/dim]")
        else:
            dev_choice = Prompt.ask(
                "Select device index",
                choices=[str(i) for i in range(1, len(detected_devices) + 1)],
                default="1",
            )
            selected_serial = detected_devices[int(dev_choice) - 1][0]
    else:
        console.print(
            "[bold yellow]⚠ No connected device detected right now.[/bold yellow]\n"
            "[dim]Artemis will auto-detect your device when you connect it later.[/dim]"
        )

    # Preserve unrelated dotenv variables and existing runtime/device settings.
    env_path = get_env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    from dotenv import dotenv_values

    existing_values = dotenv_values(stream=__import__("io").StringIO(existing_env))
    updates = {env_key: api_key.strip()}
    for key, value in {
        "ADB_HOST": "127.0.0.1",
        "ADB_PORT": "5037",
        "ARTEMIS_DEFAULT_PROFILE": "pro",
        "ARTEMIS_TRACES_DIR": "./traces",
    }.items():
        if key not in existing_values:
            updates[key] = value
    if selected_serial:
        updates["ADB_DEVICE_SERIAL"] = selected_serial
    _protected_write(env_path, _merge_env(existing_env, updates).encode("utf-8"))

    # 5. Configure MCP for AI IDEs
    console.print("\n[bold]Step 5: Connect Artemis to your AI IDE (MCP)[/bold]")
    console.print("  [1] Antigravity / Jetski (Recommended - Default)")
    console.print("  [2] Cursor")
    console.print("  [3] Claude Desktop / Claude Code")
    console.print("  [4] Windsurf")
    console.print("  [5] VS Code / Cline / Roo Code")
    console.print("  [6] Codex")
    console.print("  [7] All supported IDEs")
    console.print("  [8] Skip for now")
    mcp_choice = Prompt.ask(
        "Select IDE for MCP auto-install",
        choices=["1", "2", "3", "4", "5", "6", "7", "8"],
        default="1",
    )
    if mcp_choice != "8":
        target_map = {
            "1": "antigravity",
            "2": "cursor",
            "3": "claude",
            "4": "windsurf",
            "5": "vscode",
            "6": "codex",
            "7": "all",
        }
        target = target_map.get(mcp_choice, "antigravity")
        try:
            from artemis.interfaces.cli.commands.mcp import install_mcp_config
            from mcp_server.utils import env_utils

            proj_root = env_utils.get_project_root()
            py_exe = env_utils.resolve_python_executable(proj_root)
            installed_paths = install_mcp_config(target, py_exe, proj_root)
            console.print(
                f"[bold green]✔ Configured Artemis MCP in {len(installed_paths)} IDE location(s).[/bold green]"
            )
        except Exception as e:
            console.print(f"[yellow]Could not auto-install MCP config: {e}[/yellow]")

    console.print()
    console.print(
        Panel(
            f"[bold green]🎉 Setup completed successfully![/bold green]\n\n"
            f"Configured [bold cyan]{provider_name}[/bold cyan] in [dim]{env_path}[/dim]\n\n"
            "Try running your first task:\n"
            '  [bold cyan]uv run artemis run "Open YouTube and search for Lo-Fi Hip Hop"[/bold cyan]\n\n'
            "Or connect/update your AI IDE anytime with:\n"
            "  [bold cyan]uv run artemis mcp --install all[/bold cyan]\n\n"
            "Or run health diagnostics anytime with:\n"
            "  [bold cyan]uv run artemis doctor[/bold cyan]",
            title="☕ Ready to Go!",
            expand=False,
        )
    )
    console.print()
