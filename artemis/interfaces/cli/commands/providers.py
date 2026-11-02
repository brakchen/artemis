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

"""CLI for registered OpenAI-compatible and Anthropic Messages providers."""

from __future__ import annotations

from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from artemis.llm.providers import provider_registry

providers_app = typer.Typer(
    name="providers",
    help="Manage OpenAI-compatible and Anthropic Messages providers (API keys, base URLs, default provider).",
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=True,
)

console = Console()


def _print_table(default_name: str | None) -> None:
    entries = provider_registry.list()
    table = Table(title="LLM Providers", show_lines=False)
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("default", justify="center")
    table.add_column("kind")
    table.add_column("api_base")
    table.add_column("model")
    table.add_column("api_key")
    table.add_column("enabled", justify="center")

    if not entries:
        console.print("[yellow]No custom LLM providers registered.[/yellow]")
        console.print(
            "[dim]Add one with:[/dim] artemis providers add "
            "--name my-proxy --api-key sk-xxx --base-url https://api.example.com/v1 --model gpt-4o"
        )
    for entry in entries:
        table.add_row(
            entry.name,
            "★" if entry.name == default_name else "",
            entry.kind,
            entry.api_base or "-",
            entry.model or "-",
            entry.masked_key(),
            "yes" if entry.enabled else "no",
        )
    console.print(table)
    console.print(f"[dim]Registry file: {provider_registry.path}[/dim]")
    if default_name:
        console.print(f"[green]Default provider:[/green] {default_name}")
    else:
        console.print("[yellow]No default provider set.[/yellow]")


@providers_app.command(name="list", help="List registered LLM providers (API keys are masked).")
def list_command() -> None:
    _print_table(provider_registry.default_provider_name)


def _draft_values(draft: dict) -> dict:
    values = {key: value for key, value in draft.items() if value is not None}
    if "vendor" in values and str(values["vendor"]).lower() in {"generic", "none", ""}:
        values["vendor"] = None
    return values


def _candidate(name: str | None, **draft):
    try:
        return provider_registry.candidate(name=name, **_draft_values(draft))
    except ValueError:
        console.print(
            "[red]Invalid provider configuration. Check protocol, vendor, endpoint and key.[/red]"
        )
        raise typer.Exit(code=1) from None


@providers_app.command(
    name="edit", help="Edit a provider; omitted fields and blank keys are retained."
)
@providers_app.command(
    name="add", help="Register or update an OpenAI or Anthropic Messages provider."
)
def add_command(
    name: Annotated[str, typer.Option("--name", "-n")],
    api_key: Annotated[str | None, typer.Option("--api-key", "-k", hide_input=True)] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", "-b")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    fallback_model: Annotated[str | None, typer.Option("--fallback-model")] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="openai or anthropic")] = None,
    vendor: Annotated[
        str | None,
        typer.Option(
            "--vendor", help="minimax for MiniMax capabilities; generic clears the vendor"
        ),
    ] = None,
    temperature: Annotated[float | None, typer.Option("--temperature")] = None,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    is_multimodal: Annotated[bool | None, typer.Option("--multimodal/--text-only")] = None,
    clear_api_key: Annotated[
        bool, typer.Option("--clear-api-key", help="Explicitly clear the stored key.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm key clearing.")] = False,
    make_default: Annotated[bool, typer.Option("--default/--no-default")] = False,
    test: Annotated[bool, typer.Option("--test/--no-test")] = True,
) -> None:
    if clear_api_key and api_key and api_key.strip():
        console.print("[red]--api-key and --clear-api-key are mutually exclusive.[/red]")
        raise typer.Exit(code=1)
    if clear_api_key and not yes:
        typer.confirm(f"Clear the stored API key for '{name}'?", abort=True)
    if api_key is None and provider_registry.get(name) is None and not clear_api_key:
        api_key = typer.prompt("API key", hide_input=True)
    entry = _candidate(
        name,
        api_key=api_key,
        api_base=base_url,
        model=model,
        fallback_model=fallback_model,
        kind=kind,
        vendor=vendor,
        temperature=temperature,
        timeout_seconds=timeout,
        enabled=enabled,
        is_multimodal=is_multimodal,
        clear_api_key=clear_api_key,
    )
    if test and not clear_api_key:
        _test_credentials(entry)
    try:
        draft = {
            "api_key": api_key,
            "api_base": base_url,
            "model": model,
            "fallback_model": fallback_model,
            "kind": kind,
            "vendor": vendor,
            "temperature": temperature,
            "timeout_seconds": timeout,
            "enabled": enabled,
            "is_multimodal": is_multimodal,
            "clear_api_key": clear_api_key,
        }
        entry = provider_registry.save_candidate(
            name=name,
            make_default=make_default,
            **_draft_values(draft),
        )
    except ValueError:
        console.print("[red]Could not save provider configuration.[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Provider '{entry.name}' saved.[/green]")
    if entry.vendor == "minimax":
        console.print(
            f"Complete the role migration with: artemis init --provider minimax --name {entry.name}"
        )
    _print_table(provider_registry.default_provider_name)


@providers_app.command(
    name="models", help="List catalog models; does not prove image/tools/search permissions."
)
def models_command(
    name: Annotated[str | None, typer.Argument()] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", "-b")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", "-k", hide_input=True)] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    vendor: Annotated[str | None, typer.Option("--vendor")] = None,
) -> None:
    import asyncio
    from artemis.llm.providers import discover_models

    entry = _candidate(name, api_base=base_url, api_key=api_key, kind=kind, vendor=vendor)
    if not entry.api_base:
        console.print("[red]Provide a registered provider name or --base-url.[/red]")
        raise typer.Exit(code=1)
    try:
        models, endpoint = asyncio.run(
            discover_models(
                entry.api_base,
                entry.api_key.get_secret_value(),
                kind=entry.kind,
                vendor=entry.vendor,
            )
        )
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Model discovery failed: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]{len(models)} model(s) from[/green] {endpoint}")
    for model in models:
        console.print(f"  • {model.id}" + (f"  [dim]({model.name})[/dim]" if model.name else ""))


@providers_app.command(name="set-default", help="Mark a registered provider as the default one.")
def set_default_command(
    name: Annotated[str, typer.Argument(help="Provider name to use as default.")],
) -> None:
    try:
        entry = provider_registry.set_default(name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✔ Default LLM provider is now '{entry.name}'.[/green]")


@providers_app.command(name="remove", help="Remove a registered provider.")
def remove_command(
    name: Annotated[str, typer.Argument(help="Provider name to remove.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    if not yes:
        typer.confirm(f"Remove provider '{name}'?", abort=True)
    if provider_registry.remove(name):
        console.print(f"[green]✔ Provider '{name}' removed.[/green]")
    else:
        console.print(f"[yellow]No provider named '{name}'.[/yellow]")
        raise typer.Exit(code=1)


@providers_app.command(
    name="test", help="Verify catalog authentication using the complete provider draft."
)
def test_command(
    name: Annotated[str | None, typer.Argument()] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", "-b")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", "-k", hide_input=True)] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    vendor: Annotated[str | None, typer.Option("--vendor")] = None,
) -> None:
    _test_credentials(
        _candidate(name, api_base=base_url, api_key=api_key, kind=kind, vendor=vendor)
    )


def _test_credentials(entry) -> None:
    import asyncio
    from artemis.utils.credentials_validator import validate_api_key

    ok, message = asyncio.run(
        validate_api_key(
            provider=entry.kind,
            api_key=entry.api_key.get_secret_value(),
            base_url=entry.api_base,
            vendor=entry.vendor,
        )
    )
    console.print(message, markup=False)
    if not ok:
        raise typer.Exit(code=1)
