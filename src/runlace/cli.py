"""The ``runlace`` command line.

``init`` sets a home up, ``serve`` exposes it to an MCP host, ``sync``
re-discovers and reports what that cost you. ``add``, ``remove`` and ``import``
change the connector list afterwards, one server at a time, without the
wholesale replacement ``init`` does.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from .ai import AiFailed, complete
from .config import (
    _ENV_REF,
    Connector,
    MissingEnvVars,
    default_config_sources,
    load_env_file,
)
from .connect_cmd import (
    LiteralSecret,
    add_connectors,
    build_connector,
    fetch_open_webui,
    parse_open_webui,
    remove_connector,
    unresolved_references,
)
from .init_cmd import InitReport, format_table, run_init
from .model import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    Model,
    read_model,
    write_model,
)
from .paths import RunlacePaths, paths as runlace_paths
from .queue import APPROVAL_ASK, APPROVAL_MODES
from .server import serve as serve_server
from .sync_cmd import format_broken, format_changes, run_sync
from .typecheck import check_stubs

OPEN_WEBUI_TOKEN_ENV = "OPEN_WEBUI_TOKEN"

# Every command that re-discovers needs this, not just `serve`. A `${VAR}` is
# resolved by whatever process opens the connection, so running `runlace remove`
# in a shell without the tokens exported makes every other server "not answer"
# -- and a server that did not answer has its tools pruned.
EnvFiles = Annotated[
    list[Path] | None,
    typer.Option(
        "--env-file",
        help="File of KEY=VALUE lines holding the tokens your connectors "
        "reference. Repeatable. Values are never printed.",
        show_default=False,
    ),
]

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
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model workflows reach through ctx.ai(...), e.g. qwen3:8b. "
            "Same thing as `runlace model set`.",
            show_default=False,
        ),
    ] = None,
    model_base_url: Annotated[
        str, typer.Option("--model-base-url", help="Where that model lives.")
    ] = DEFAULT_BASE_URL,
    env_file: EnvFiles = None,
) -> None:
    """Set up ~/.runlace, discover your MCP servers, and generate typed stubs."""
    paths = runlace_paths()
    _load_env_files(env_file)
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

    _init_model(paths, model, model_base_url, assume_yes=yes)

    if verify:
        result = check_stubs(paths.types)
        typer.echo("")
        typer.echo(result.report())
        if not result.ok:
            raise typer.Exit(code=1)


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="What to call it: ctx.<name> in a workflow.")],
    command: Annotated[
        str | None,
        typer.Option("--command", help="Binary to launch for a stdio server, e.g. npx."),
    ] = None,
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", help="One argument for --command. Repeatable, in order."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="KEY=VALUE for a stdio server. Repeatable."),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Endpoint of a remote MCP server.")
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option("--header", help="'Name: value' for --url. Repeatable."),
    ] = None,
    transport: Annotated[
        str, typer.Option("--transport", help="http or sse, for --url.")
    ] = "http",
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
    env_file: EnvFiles = None,
) -> None:
    """Add one MCP server to ~/.runlace, keeping the ones already there."""
    paths = _existing_home()
    _load_env_files(env_file)
    try:
        connector = build_connector(
            name,
            command=command,
            args=list(arg or []),
            env=_pairs(env, "--env", "="),
            url=url,
            headers=_pairs(header, "--header", ":"),
            transport="sse" if transport == "sse" else "http",
        )
    except LiteralSecret as exc:
        typer.secho(f"{name}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _apply([connector], paths, timeout=timeout)


@app.command("import")
def import_(
    from_open_webui: Annotated[
        str,
        typer.Option(
            "--from-open-webui",
            help="Base URL of an Open WebUI instance, e.g. http://localhost:3000.",
        ),
    ],
    token_env: Annotated[
        str,
        typer.Option(
            "--token-env",
            help="Environment variable holding an Open WebUI ADMIN token.",
        ),
    ] = OPEN_WEBUI_TOKEN_ENV,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
    env_file: EnvFiles = None,
) -> None:
    """Copy a chat UI's MCP servers into ~/.runlace.

    The UI keeps the credentials; this writes a ${VAR} reference beside each one
    and tells you what to export. Runlace itself is skipped, and so is anything
    the UI serves over OpenAPI rather than MCP.
    """
    paths = _existing_home()
    _load_env_files(env_file)
    token = os.environ.get(token_env, "")
    if not token:
        typer.secho(
            f"${token_env} is not set. In Open WebUI: your avatar -> Settings -> "
            f"Account -> API keys, then export it. It needs to be an admin token.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        payload = fetch_open_webui(from_open_webui, token)
    except Exception as exc:  # network, auth, HTML instead of JSON
        typer.secho(f"{from_open_webui}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    imported = parse_open_webui(payload)
    for warning in imported.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    for name in imported.skipped:
        typer.echo(f"skip     {name} (that's me)")
    if not imported.connectors:
        typer.secho("Nothing to import.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    _apply(imported.connectors, paths, timeout=timeout)
    typer.echo("")
    typer.echo(
        "These are now driven through Runlace. Disable them in Open WebUI unless "
        "you want the model calling them directly, without the confirm gate."
    )


@app.command()
def remove(
    name: Annotated[str, typer.Argument(help="Connector to drop.")],
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
    env_file: EnvFiles = None,
) -> None:
    """Drop one MCP server from ~/.runlace.

    Stored workflows that used it stay readable. Their next run is refused
    rather than half-executed.
    """
    paths = _existing_home()
    _load_env_files(env_file)
    result, report = remove_connector(paths, name, timeout=timeout)
    if report is None:
        for warning in result.warnings:
            typer.secho(warning, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"-        {name}")
    _print_report(report)


@app.command()
def sync(
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each server to answer tools/list.")
    ] = 30.0,
    env_file: EnvFiles = None,
) -> None:
    """Re-discover your MCP servers and report which stored workflows broke."""
    paths = runlace_paths()
    _load_env_files(env_file)
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
    approval: Annotated[
        str,
        typer.Option(
            "--approval",
            help="What a side-effecting run does without confirm: "
            "ask (park it for a human) or allow (run it).",
        ),
    ] = APPROVAL_ASK,
    env_file: EnvFiles = None,
) -> None:
    """Start the Runlace MCP server so any MCP host can add it.

    `--approval` is the one gate the agent cannot open for itself. It belongs to
    whoever runs this command, which is the developer embedding Runlace, not the
    person their product is talking to -- see the v2 chapter of SPEC.md.
    """
    paths = runlace_paths()
    if approval not in APPROVAL_MODES:
        typer.secho(
            f"--approval must be one of {', '.join(APPROVAL_MODES)}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    _load_env_files(env_file)
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
    serve_server(paths, port=http, host=host, approval=approval)


def _init_model(
    paths: RunlacePaths, name: str | None, base_url: str, *, assume_yes: bool
) -> None:
    """Offer to pick a model during init. Never blocks setting a home up.

    Skipping is fine: workflows that do not call `ctx.ai(...)` never need one,
    and `runlace model set` exists for later. What is not fine is finding out
    only when a workflow is refused, so a home without a model says so.
    """
    if name is None:
        if assume_yes or read_model(paths.model) is not None:
            return
        typer.echo("")
        name = typer.prompt(
            "Model for ctx.ai(...) steps, blank to skip",
            default="",
            show_default=False,
        ).strip()
        if not name:
            typer.secho(
                "  No model. Workflows calling ctx.ai() are refused until "
                "`runlace model set <name>`.",
                fg=typer.colors.BRIGHT_BLACK,
            )
            return

    model = Model(base_url=base_url, model=name)
    write_model(paths.model, model)
    typer.echo("")
    typer.echo(f"Model: {model.model}  {model.base_url}")
    _describe(model)


model_app = typer.Typer(
    add_completion=False,
    help="The model `ctx.ai(...)` calls. One per machine, chosen here.",
    no_args_is_help=True,
)
app.add_typer(model_app, name="model")


@model_app.command("set")
def model_set(
    name: Annotated[
        str,
        typer.Argument(help="Model name as the backend spells it, e.g. qwen3:8b."),
    ],
    base_url: Annotated[
        str,
        typer.Option(
            "--base-url",
            help="OpenAI-compatible endpoint. Ollama, LiteLLM, OpenRouter, vLLM "
            "all expose one.",
        ),
    ] = DEFAULT_BASE_URL,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help='Pass a reference, not the key itself: "${OPENROUTER_API_KEY}".',
            show_default=False,
        ),
    ] = None,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for one completion.")
    ] = DEFAULT_TIMEOUT,
    check: Annotated[
        bool, typer.Option(help="Ask the model to say hello before saving.")
    ] = True,
    env_file: EnvFiles = None,
) -> None:
    """Choose the model workflows reach through `ctx.ai(...)`.

    Set here and not in workflow code: the agent writing a workflow is not the
    one paying for inference or answering for where the data went, and a
    workflow that hard-codes a model breaks the day you switch to a local one.
    """
    paths = _existing_home()
    _load_env_files(env_file)
    if api_key and not _ENV_REF.search(api_key):
        typer.secho(
            str(
                LiteralSecret(
                    "--api-key", '"${RUNLACE_MODEL_KEY}" (or any name you like)'
                )
            ).replace("config.json", "model.json"),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    model = Model(base_url=base_url, model=name, api_key=api_key, timeout=timeout)
    if check:
        typer.echo(f"Asking {name} at {base_url} to answer once...")
        try:
            answer = asyncio.run(
                complete(
                    model.resolved(),
                    [{"role": "user", "content": "Reply with the single word: ready"}],
                )
            )
        except (AiFailed, MissingEnvVars) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            typer.secho(
                "Nothing was saved. Re-run with --no-check to save it anyway.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.secho(f"  {answer.text.strip()[:80]}", fg=typer.colors.BRIGHT_BLACK)

    write_model(paths.model, model)
    typer.echo(f"Saved to {paths.model}")
    _describe(model)


@model_app.command("show")
def model_show() -> None:
    """Print the configured model. Never prints a resolved key."""
    paths = _existing_home()
    model = read_model(paths.model)
    if model is None:
        typer.secho(
            "No model configured. `runlace model set <name>` picks one; "
            "workflows calling ctx.ai() are refused until then.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    typer.echo(f"{model.model}  {model.base_url}")
    _describe(model)


def _describe(model: Model) -> None:
    """The one thing about a model that changes how a workflow behaves."""
    if model.is_local:
        typer.echo("  local: AI steps count as reads and run without approval.")
    else:
        typer.echo(
            "  remote: AI steps count as side effects, because the data leaves "
            "this machine. Workflows using them park for approval."
        )
    if model.api_key:
        # The reference, deliberately -- the value is only ever in the environment.
        typer.echo(f"  key: {model.api_key}")


def _existing_home() -> RunlacePaths:
    """The Runlace home, or a clear error. Every command below needs one."""
    paths = runlace_paths()
    if not paths.config.exists():
        typer.secho(
            f"No Runlace home at {paths.home}. Run `runlace init` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return paths


def _load_env_files(paths: list[Path] | None) -> None:
    """Load each --env-file, reporting names only. Never the values."""
    for path in paths or []:
        if not path.exists():
            typer.secho(f"{path}: not found", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        names = load_env_file(path)
        typer.secho(
            f"{path}: loaded {', '.join(names) if names else 'nothing new'}",
            fg=typer.colors.BRIGHT_BLACK,
            err=True,
        )


def _pairs(values: list[str] | None, flag: str, sep: str) -> dict[str, str]:
    """Parse repeated ``KEY<sep>VALUE`` options. Only the first separator splits."""
    parsed: dict[str, str] = {}
    for raw in values or []:
        key, found, value = raw.partition(sep)
        if not found or not key.strip():
            typer.secho(
                f"{flag} expects KEY{sep}VALUE, got `{raw}`",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        parsed[key.strip()] = value.strip()
    return parsed


def _apply(incoming: list[Connector], paths: RunlacePaths, *, timeout: float) -> None:
    """Merge, re-discover, and print what happened. Shared by add and import."""
    merged, report = add_connectors(paths, incoming, timeout=timeout)
    for warning in merged.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    for name in merged.added:
        typer.echo(f"+        {name}")
    for name in merged.replaced:
        typer.echo(f"~        {name} (replaced)")
    if not merged.changed:
        typer.secho("Nothing added.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    for name, variables in unresolved_references(merged.connectors).items():
        typer.secho(
            f"warning: {name} needs {', '.join(variables)}, not set right now",
            fg=typer.colors.YELLOW,
        )
    _print_report(report)


def _print_report(report: InitReport) -> None:
    typer.echo("")
    typer.echo(format_table(report.rows) if report.rows else "No MCP servers left.")
    typer.echo("")
    typer.echo(
        f"{len(report.connected)} connector(s) connected, {report.tool_count} tool(s)"
    )
    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)

    failed = [r for r in report.rows if r.status != "connected"]
    if failed:
        typer.secho(
            f"{len(failed)} connector(s) did not answer: "
            f"{', '.join(r.name for r in failed)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


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
