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

"""CLI for the user-configured LLM provider registry (OpenAI-compatible)."""

from typing import Annotated, Optional

from rich.console import Console
from rich.table import Table
import typer

from artemis.llm.providers import provider_registry

providers_app = typer.Typer(
    name="providers",
    help="Manage OpenAI-compatible LLM providers (API keys, base URLs, default provider).",
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


@providers_app.command(name="add", help="Register (or update) an OpenAI-compatible LLM provider.")
def add_command(
    name: Annotated[str, typer.Option("--name", "-n", help="Provider name, e.g. my-proxy.")],
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", "-k", help="API key (prompted when omitted).", hide_input=True),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            "-b",
            help="OpenAI-compatible base URL, e.g. https://api.example.com/v1",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Default model for this provider, e.g. gpt-4o."),
    ] = None,
    fallback_model: Annotated[
        str | None,
        typer.Option(
            "--fallback-model", help="Fallback model (like artemis.jsonc default.fallback)."
        ),
    ] = None,
    kind: Annotated[
        str,
        typer.Option("--kind", help="Wire format. Only 'openai' (OpenAI-compatible) today."),
    ] = "openai",
    temperature: Annotated[
        float | None, typer.Option("--temperature", help="Optional sampling temperature.")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Optional request timeout in seconds.")
    ] = None,
    make_default: Annotated[
        bool,
        typer.Option("--default/--no-default", help="Make this the default LLM provider."),
    ] = False,
    test: Annotated[
        bool,
        typer.Option("--test/--no-test", help="Verify the key against the endpoint before saving."),
    ] = True,
) -> None:
    if not api_key:
        api_key = typer.prompt("API key", hide_input=True)
    api_key = api_key.strip()
    if not api_key:
        console.print("[red]API key must not be empty.[/red]")
        raise typer.Exit(code=1)

    if test:
        _test_credentials(name=name, api_key=api_key, base_url=base_url)

    try:
        entry = provider_registry.add(
            name=name,
            api_key=api_key,
            api_base=base_url,
            model=model,
            fallback_model=fallback_model,
            kind=kind,
            temperature=temperature,
            timeout_seconds=timeout,
            make_default=make_default,
        )
    except ValueError as exc:
        console.print(f"[red]Could not save provider: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✔ Provider '{entry.name}' saved.[/green]")
    _print_table(provider_registry.default_provider_name)


@providers_app.command(
    name="models",
    help="List the models an OpenAI-compatible endpoint exposes (GET {base}/models).",
)
def models_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Registered provider name. Omit to use --base-url/--api-key."),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", "-b", help="OpenAI-compatible base URL.")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", "-k", help="API key (only needed for unregistered endpoints)."),
    ] = None,
) -> None:
    import asyncio

    from artemis.llm.providers import discover_models

    kind = "openai"
    if name:
        entry = provider_registry.get(name)
        if entry is None:
            console.print(f"[red]Unknown provider '{name}'.[/red]")
            raise typer.Exit(code=1)
        base_url = base_url or entry.api_base
        api_key = api_key or entry.api_key.get_secret_value()
        kind = entry.kind

    if not base_url:
        console.print("[red]Provide a registered provider name or --base-url.[/red]")
        raise typer.Exit(code=1)

    try:
        models, endpoint = asyncio.run(discover_models(base_url, api_key, kind=kind))
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Model discovery failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✔ {len(models)} model(s) from[/green] {endpoint}")
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


@providers_app.command(name="test", help="Verify a registered provider's key against its endpoint.")
def test_command(
    name: Annotated[str, typer.Argument(help="Provider name to test.")],
) -> None:
    entry = provider_registry.get(name)
    if entry is None:
        console.print(f"[red]Unknown provider '{name}'.[/red]")
        raise typer.Exit(code=1)
    _test_credentials(
        name=entry.name, api_key=entry.api_key.get_secret_value(), base_url=entry.api_base
    )


def _test_credentials(*, name: str, api_key: str, base_url: str | None) -> None:
    import asyncio

    from artemis.utils.credentials_validator import validate_api_key

    console.print(f"[dim]Testing provider '{name}' against {base_url or 'api.openai.com'}...[/dim]")
    ok, message = asyncio.run(
        validate_api_key(provider="openai", api_key=api_key, base_url=base_url)
    )
    if ok:
        console.print(f"[green]✔ {message}[/green]")
    else:
        console.print(f"[red]✘ {message}[/red]")
        raise typer.Exit(code=1)
