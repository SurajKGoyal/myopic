"""
myopic setup wizard — interactive first-run configuration.

Mirrors amnesic's wizard UX: prompt for the connection, verify it live, then
persist it — the URL to config.toml (token referenced as ${GITLAB_TOKEN}) and
the token value to a sibling .env at chmod 600, so secrets never sit in the TOML.

Public API:
  run_wizard(welcome)        — interactive flow (prompt → test → save)
  test_connection(url, token)— returns (ok, username_or_error)
  write_config_toml(url)     — write config.toml with ${GITLAB_TOKEN} reference
  upsert_env_var(name, value)— insert/replace NAME=VALUE in .env, chmod 600
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from myopic.config import config_dir, config_path, env_path, invalidate_config_cache

_CONFIG_TEMPLATE = """\
# myopic config.toml
# Documentation: https://github.com/SurajKGoyal/myopic
#
# The token is kept OUT of this file — it's referenced as ${{GITLAB_TOKEN}} and
# its value lives in the sibling .env (chmod 600). Edit the URL here if your
# GitLab instance moves; rotate the token with `myopic set-secret`.

[gitlab]
url = "{url}"
token = "${{GITLAB_TOKEN}}"
"""


def _token_hint(url: str) -> str:
    return f"{url.rstrip('/')}/-/user_settings/personal_access_tokens"


def secure_file(path: Path) -> None:
    """Best-effort chmod 600 — owner read/write only. No-op where unsupported."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def test_connection(url: str, token: str) -> tuple[bool, str]:
    """Authenticate to GitLab. Returns (ok, username) or (False, error_message)."""
    try:
        import gitlab

        client = gitlab.Gitlab(url=url, private_token=token)
        client.auth()
        user = client.user
        return True, getattr(user, "username", "?") if user else "?"
    except Exception as exc:
        # Never surface the raw error — some python-gitlab versions echo the token.
        first = str(exc).splitlines()[0][:120]
        redacted = first.replace(token, "***") if token else first
        return False, redacted


def write_config_toml(url: str) -> None:
    """Write config.toml with the URL and a ${GITLAB_TOKEN} reference."""
    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(_CONFIG_TEMPLATE.format(url=url.rstrip("/")), encoding="utf-8")


def upsert_env_var(name: str, value: str) -> None:
    """Insert or replace NAME=VALUE in the .env, preserving other lines. chmod 600."""
    config_dir().mkdir(parents=True, exist_ok=True)
    path = env_path()

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if (
            stripped and not stripped.startswith("#") and "=" in stripped
            and stripped.split("=", 1)[0].strip() == name
        ):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")

    content = "\n".join(out)
    if content and not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    secure_file(path)


def run_wizard(welcome: bool = True) -> None:
    """Interactive GitLab setup: prompt → verify live → persist config + secret."""
    config_dir().mkdir(parents=True, exist_ok=True)

    if welcome:
        click.echo()
        click.echo("Welcome to myopic — the code-review MCP that sees the whole codebase.")
        click.echo("Let's connect it to your GitLab.")
        click.echo()

    while True:
        url = click.prompt("? GitLab URL", default="https://gitlab.com").rstrip("/")
        click.echo()
        click.echo("  Create a personal access token with 'api' (or 'read_api') scope:")
        click.echo(f"    {_token_hint(url)}")
        token = click.prompt("? Personal access token (hidden)", hide_input=True)

        click.echo()
        click.echo("  Testing connection...", nl=False)
        ok, info = test_connection(url, token)

        if ok:
            click.echo(f" ✓ Authenticated as {info}.")
            click.echo()
            write_config_toml(url)
            click.echo(f"  ✓ URL saved to {config_path()}")
            upsert_env_var("GITLAB_TOKEN", token)
            click.echo(f"  ✓ Token saved to {env_path()} (chmod 600)")
            invalidate_config_cache()
            break

        click.echo(" ✗ Failed.")
        click.echo(f"  Error: {info}")
        click.echo()
        if not click.confirm("? Try again with different details?", default=True):
            raise SystemExit(1)
        click.echo()

    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Run `myopic test` to re-verify the connection any time.")
    click.echo("  2. Add myopic to your MCP client config (see the README snippet).")
    click.echo("  3. Ask your AI client to review a merge request URL.")
    click.echo()
