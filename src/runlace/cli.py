"""The ``runlace`` command line.

Three commands: ``init`` sets a home up, ``serve`` exposes it to an MCP host,
``sync`` re-discovers and reports what that cost you.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .config import default_config_sources
from .init_cmd import format_table, run_init
from .paths import paths as runlace_paths
from .server import serve as serve_server
from .sync_cmd import format_broken, format_changes, run_sync
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
def sync(
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
) -> None:
    """Re-discover your MCP servers and report which stored workflows broke."""
    paths = runlace_paths()
    if not paths.config.exists():
        typer.secho(
            f"No Runlace home at {paths.home}. Run `runlace init` first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Runlace home: {paths.home}")
    report = run_sync(paths, timeout=timeout)

    typer.echo("")
    typer.echo(format_table(report.rows) if report.rows else "No MCP servers found.")

    counts = report.counts()
    typer.echo("")
    typer.echo(
        f"{counts['added']} tool(s) added, {counts['removed']} removed, "
        f"{counts['changed']} changed their schema"
    )
    if report.changes:
        typer.echo(format_changes(report.changes))

    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    for name in report.unreachable:
        typer.secho(
            f"warning: {name} did not answer, so its tools now read as removed. "
            f"Fix the server and sync again before recreating anything.",
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    if report.ok:
        typer.secho(
            f"{report.workflows_checked} workflow(s) checked, all still runnable.",
            fg=typer.colors.GREEN,
        )
        return

    typer.secho(
        f"{len(report.broken)} of {report.workflows_checked} workflow(s) would now "
        f"be refused:",
        fg=typer.colors.RED,
    )
    typer.echo(format_broken(report.broken))
    typer.echo("")
    typer.echo(
        "Their pinned versions are untouched and still readable with "
        "`get_workflow`. Review what changed, then create a new version of each."
    )
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
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Interface to bind --http to. Use 0.0.0.0 to let a container reach it.",
        ),
    ] = "127.0.0.1",
) -> None:
    """Start the Runlace MCP server so any MCP host can add it."""
    paths = runlace_paths()
    if not paths.db.exists():
        typer.secho(
            f"No Runlace home at {paths.home}. Run `runlace init` first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if http is not None and host != "127.0.0.1":
        # Loud on purpose. Anything that can reach this port can run a stored
        # workflow, and `confirm=True` is one JSON field away.
        typer.secho(
            f"Serving on {host}:{http} -- reachable from outside this machine. "
            f"Anything that can reach it can run your workflows.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    # stdio is the transport: anything printed to stdout would corrupt it.
    serve_server(paths, port=http, host=host)


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
