"""
myopic CLI

Commands:
  myopic init               — interactive setup wizard (prompt → test → save)
  myopic init --template    — write a blank config template for hand-editing
  myopic set-secret         — set or rotate the GitLab token (hidden input)
  myopic test               — verify the configured GitLab connection
  myopic doctor             — health-check config + the semantic layer (Ollama)
  myopic index [PATH]       — build/refresh the semantic index (cron-friendly)
  (no subcommand)           — start the MCP server when launched by a client
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from myopic.config import config_dir, config_path

console = Console()

_TOKEN_HINT = "https://gitlab.com/-/user_settings/personal_access_tokens"

_TEMPLATE = """\
# myopic config.toml
# Documentation: https://github.com/SurajKGoyal/myopic
#
# Configure the platform(s) you review on. Keep tokens OUT of this file —
# reference them via ${VAR} and put the values in a sibling .env (config dir /
# .env, chmod 600), or export them in your environment.

# GitLab — needs a token with `api` (or `read_api`) scope.
[gitlab]
url = "https://gitlab.com"
token = "${GITLAB_TOKEN}"

# GitHub — needs a token with repo / pull-request read access. Uncomment to use.
# The `url` is optional: set it only for GitHub Enterprise (its base host),
# e.g. "https://github.mycompany.com". Public github.com needs no url.
# [github]
# token = "${GITHUB_TOKEN}"
# url = "https://github.mycompany.com"
"""


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """myopic — the code-review MCP that sees the whole codebase, not just the diff.

    With no subcommand:
      • If stdin is piped (an MCP client launched us) → start the MCP server.
      • If stdin is a TTY (you ran 'myopic' in your terminal) → show this help.
    """
    if ctx.invoked_subcommand is not None:
        return

    import sys

    if sys.stdin.isatty():
        click.echo(ctx.get_help())
        return

    from myopic.server import main as _server_main

    _server_main()


@cli.command()
@click.option("--template", is_flag=True, default=False,
              help="Write a blank config template for hand-editing instead of running the wizard.")
def init(template: bool) -> None:
    """Set up myopic interactively (wizard). Pass --template to hand-edit instead.

    The wizard prompts for your GitLab URL and token, verifies the connection
    live, then saves the URL to config.toml and the token to a sibling .env
    (chmod 600) so the secret never lives in the TOML.
    """
    cfg_dir = config_dir()
    cfg_file = config_path()

    if template:
        if cfg_file.exists():
            console.print(
                f"[yellow]Config already exists:[/yellow] {cfg_file}\n"
                f"Edit it directly, or delete it and re-run."
            )
            raise SystemExit(0)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(_TEMPLATE, encoding="utf-8")
        console.print(f"[green]Created:[/green] {cfg_file}")
        console.print()
        console.print("[bold]Next steps:[/bold]")
        console.print(f"  1. Create a GitLab token (api scope): [cyan]{_TOKEN_HINT}[/cyan]")
        console.print(f"  2. Put it in [cyan]{cfg_dir / '.env'}[/cyan] as [cyan]GITLAB_TOKEN=...[/cyan]")
        console.print(f"  3. Edit [cyan]{cfg_file}[/cyan] if your GitLab URL is self-hosted")
        console.print(f"  4. Run [cyan]myopic test[/cyan] to verify the connection")
        return

    if cfg_file.exists():
        if not click.confirm(
            f"Config already exists at {cfg_file}. Reconfigure?", default=False
        ):
            console.print(
                "Left unchanged. Use [cyan]myopic set-secret[/cyan] to rotate just the token."
            )
            raise SystemExit(0)

    from myopic._wizard import run_wizard
    run_wizard(welcome=True)


@cli.command("set-secret")
def set_secret() -> None:
    """Set or rotate the GitLab token in ~/.config/myopic/.env (hidden input)."""
    value = click.prompt("GitLab token", hide_input=True, confirmation_prompt=True)

    from myopic._wizard import upsert_env_var
    from myopic.config import invalidate_config_cache

    upsert_env_var("GITLAB_TOKEN", value)
    invalidate_config_cache()
    console.print(
        f"[green]✓[/green] Saved GITLAB_TOKEN to {config_dir() / '.env'} (chmod 600)"
    )


@cli.command()
def test() -> None:
    """Verify that the configured GitLab URL + token authenticate successfully."""
    from myopic.config import load_config

    try:
        cfg = load_config()
    except ValueError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise SystemExit(1) from exc

    try:
        import gitlab

        client = gitlab.Gitlab(url=cfg.url, private_token=cfg.token)
        client.auth()
        user = client.user
        username = getattr(user, "username", "?") if user else "?"
        console.print(
            f"[green]✓[/green] Authenticated to [cyan]{cfg.url}[/cyan] as [bold]{username}[/bold]"
        )
    except Exception as exc:
        msg = str(exc).splitlines()[0][:120]
        console.print(f"[red]✗[/red] Could not authenticate to {cfg.url}: [red]{msg}[/red]")
        raise SystemExit(1) from exc


@cli.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--force", is_flag=True, default=False,
              help="Full rebuild instead of the default incremental refresh.")
def index(root: str, force: bool) -> None:
    """Build or refresh the semantic index for a repo (incremental by default).

    myopic is a stdio server with no background process, so there is no built-in
    scheduler. This command is the hook for one: point cron/launchd at
    `myopic index /path/to/repo` to keep the index fresh out of band. Needs the
    myopic[semantic] extra + a running Ollama.
    """
    from myopic.tools.index_repo import index_repo as _index_repo

    resolved = str(Path(root).resolve())
    result = _index_repo(resolved, force=force)
    if "error" in result:
        console.print(f"[red]✗[/red] {result['error']}")
        raise SystemExit(1)
    console.print(
        f"[green]✓[/green] {result['mode']} index — {result['indexed_chunks']} chunks "
        f"({result['changed_files']} changed, {result['deleted_files']} removed) "
        f"@ {result.get('git_sha') or 'no-git'}"
    )


# ---------------------------------------------------------------------------
# doctor — health-check the setup, including the external Ollama dependency
# ---------------------------------------------------------------------------

# These wrap myopic.ollama so the shared logic lives in one place; doctor and the
# headless auto-pull path (embeddings) both use it. Kept as module-level names so
# tests can monkeypatch them.
def _ollama_models(url: str) -> list[str]:
    from myopic.ollama import list_models
    return list_models(url)


def _model_present(models: list[str], model: str) -> bool:
    from myopic.ollama import model_present
    return model_present(models, model)


def _pull_model(url: str, model: str) -> None:
    """Pull an embedding model via Ollama's HTTP API, with a progress bar."""
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn

    from myopic.ollama import pull_model

    with Progress(
        TextColumn("[cyan]{task.description}"), BarColumn(), DownloadColumn(),
        console=console,
    ) as prog:
        task = prog.add_task(f"pulling {model}", total=None)

        def _cb(status: str, completed: int | None, total: int | None) -> None:
            if total:
                prog.update(task, total=total, completed=completed or 0, description=status)
            else:
                prog.update(task, description=status)

        pull_model(url, model, on_progress=_cb)


@cli.command()
@click.option("--pull/--no-pull", "do_pull", default=None,
              help="Pull the embedding model if missing (prompts if not specified).")
def doctor(do_pull: bool | None) -> None:
    """Check myopic's setup: review-platform config + the optional semantic layer.

    The semantic layer talks to a *local Ollama server* that myopic does not
    manage — it must be installed, running, and have the embedding model pulled.
    This command verifies each link in that chain and offers to pull the model.
    """
    from myopic.config import embed_model, load_config, ollama_url

    console.print("[bold]myopic doctor[/bold]\n")
    hard_fail = False

    # 1. Review platform (required for the core tools).
    console.print("[bold]Review platform[/bold]")
    configured = False
    for platform in ("gitlab", "github"):
        try:
            cfg = load_config(platform)
            configured = True
            console.print(f"  [green]✓[/green] {platform}: configured ({cfg.url or 'github.com'})")
        except ValueError:
            console.print(f"  [dim]·[/dim] {platform}: not configured")
    if not configured:
        hard_fail = True
        console.print("  [red]✗[/red] No platform configured — run [cyan]myopic init[/cyan].")

    # 2. Optional semantic layer: extra → Ollama → model.
    console.print("\n[bold]Semantic layer (optional — graph review works without it)[/bold]")
    try:
        import httpx  # noqa: F401
        import lancedb  # noqa: F401
    except ImportError:
        console.print('  [yellow]○[/yellow] extra not installed — '
                      '[cyan]pip install "myopic[semantic]"[/cyan] to enable semantic search.')
        _doctor_exit(hard_fail)
        return
    console.print("  [green]✓[/green] extra installed (lancedb + httpx)")

    url, model = ollama_url(), embed_model()
    try:
        models = _ollama_models(url)
    except Exception as exc:
        console.print(f"  [yellow]○[/yellow] Ollama not reachable at [cyan]{url}[/cyan] "
                      f"({str(exc).splitlines()[0][:70]}).")
        console.print("     Install & start Ollama (https://ollama.com), or set "
                      "[cyan]MYOPIC_OLLAMA_URL[/cyan].")
        _doctor_exit(hard_fail)
        return
    console.print(f"  [green]✓[/green] Ollama reachable at {url}")

    if _model_present(models, model):
        console.print(f"  [green]✓[/green] embedding model pulled: [cyan]{model}[/cyan]")
    else:
        console.print(f"  [yellow]○[/yellow] embedding model not pulled: [cyan]{model}[/cyan]")
        should = do_pull
        if should is None:
            should = click.confirm("     Pull it now (~150 MB, one time)?", default=True)
        if should:
            try:
                _pull_model(url, model)
                console.print(f"  [green]✓[/green] pulled {model}")
            except Exception as exc:
                hard_fail = True
                console.print(f"  [red]✗[/red] pull failed: {str(exc).splitlines()[0][:90]}")
        else:
            console.print(f"     Skipped — pull later with [cyan]ollama pull {model}[/cyan].")

    _doctor_exit(hard_fail)


def _doctor_exit(hard_fail: bool) -> None:
    if hard_fail:
        console.print("\n[red]Some checks failed.[/red]")
        raise SystemExit(1)
    console.print("\n[green]All good.[/green]")


if __name__ == "__main__":
    cli()
