"""The ``runlace`` command line.

M1 shipped ``init``; M2 adds ``serve``. ``sync`` arrives with a later milestone
and is not stubbed out here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .config import default_config_sources
from .init_cmd import format_table, run_init
from .paths import paths as runlace_paths
from .server import serve as serve_server
from .typecheck import check_stubs

app = typer.Typer(
    add_completion=False,
    help="Deterministic, replayable workflows over your MCP servers.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Keeps `init` a subcommand; Typer would otherwise hoist a lone command."""


@app.command()
def init(
    from_: Annotated[
        list[Path] | None,
        typer.Option(
            "--from",
            help="MCP config file to import. Repeatable. Skips the prompt.",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Import every config found without asking.")
    ] = False,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
    verify: Annotated[
        bool, typer.Option(help="Typecheck the generated stubs with pyright.")
    ] = True,
) -> None:
    """Set up ~/.runlace, discover your MCP servers, and generate typed stubs."""
    paths = runlace_paths()
    sources = list(from_) if from_ else _prompt_for_sources(assume_yes=yes)
    if not sources:
        typer.secho("No MCP config selected. Nothing to import.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    typer.echo(f"Runlace home: {paths.home}")
    report = run_init(paths, sources, timeout=timeout)

    typer.echo("")
    typer.echo(format_table(report.rows) if report.rows else "No MCP servers found.")
    typer.echo("")
    typer.echo(
        f"{len(report.connected)} connector(s) connected, "
        f"{report.tool_count} tool(s), stubs in {paths.types}"
    )

    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)

    if verify:
        result = check_stubs(paths.types)
        typer.echo("")
        typer.echo(result.report())
        if not result.ok:
            raise typer.Exit(code=1)


@app.command()
def serve(
    http: Annotated[
        int | None,
        typer.Option(
            "--http",
            help="Serve over streamable HTTP on this port instead of stdio.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Start the Runlace MCP server so any MCP host can add it."""
    paths = runlace_paths()
    if not paths.db.exists():
        typer.secho(
            f"No Runlace home at {paths.home}. Run `runlace init` first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    # stdio is the transport: anything printed to stdout would corrupt it.
    serve_server(paths, port=http)


def _prompt_for_sources(*, assume_yes: bool) -> list[Path]:
    found = [path for path in default_config_sources() if path.exists()]
    if not found:
        typer.echo("No MCP config found. Pass one with --from <path>.")
        return []
    if assume_yes:
        return found
    chosen: list[Path] = []
    for path in found:
        if typer.confirm(f"Import MCP servers from {path}?", default=True):
            chosen.append(path)
    return chosen


def main() -> None:
    app()
