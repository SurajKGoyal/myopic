"""
myopic CLI

Commands:
  myopic init               — write a config template for hand-editing
  myopic test               — verify the configured GitLab connection
  (no subcommand)           — start the MCP server when launched by a client
"""

from __future__ import annotations

import click
from rich.console import Console

from myopic.config import config_dir, config_path

console = Console()

_TOKEN_HINT = "https://gitlab.com/-/user_settings/personal_access_tokens"

_TEMPLATE = """\
# myopic config.toml
# Documentation: https://github.com/SurajKGoyal/myopic
#
# myopic needs a GitLab URL and a personal access token with `api` (or at least
# `read_api`) scope. Keep the token OUT of this file — reference it via ${VAR}
# and put the value in a sibling .env file (config dir / .env), or export it in
# your environment.

[gitlab]
url = "https://gitlab.com"
token = "${GITLAB_TOKEN}"
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
def init() -> None:
    """Write a config template to the myopic config directory for hand-editing."""
    cfg_dir = config_dir()
    cfg_file = config_path()

    if cfg_file.exists():
        console.print(
            f"[yellow]Config already exists:[/yellow] {cfg_file}\n"
            f"Edit it directly, or delete it and re-run [cyan]myopic init[/cyan]."
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
    console.print(f"  5. Add myopic to your MCP client config (see README)")


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


if __name__ == "__main__":
    cli()
