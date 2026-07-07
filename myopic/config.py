"""
Configuration for myopic.

Resolves the review-platform connection (URL + access token) from, in order:
1. A TOML config file at ~/.config/myopic/config.toml (override with MYOPIC_CONFIG)
2. Environment variables (MYOPIC_GITLAB_URL / GITLAB_URL, MYOPIC_GITLAB_TOKEN / GITLAB_TOKEN)

A sibling .env file next to the config is auto-loaded so secrets never live in
the TOML itself. ${VAR} references inside the TOML are expanded from the
environment at load time.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def config_dir() -> Path:
    """Resolve myopic's config directory, honoring XDG / Windows conventions."""
    override = os.environ.get("MYOPIC_HOME")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "myopic"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "myopic"
    return Path.home() / ".config" / "myopic"


def config_path() -> Path:
    """Path to the TOML config file (override via MYOPIC_CONFIG)."""
    override = os.environ.get("MYOPIC_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.toml"


def env_path() -> Path:
    """Path to the sibling .env file that holds secrets (config dir / .env)."""
    return config_dir() / ".env"


def _load_env_file(env_path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding)."""
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _expand(value: str) -> str:
    """Expand ${VAR} references from the environment; raise if a var is missing."""
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(
                f"Config references ${{{name}}} but {name} is not set in the "
                f"environment or {config_dir() / '.env'}."
            )
        return os.environ[name]

    return _ENV_REF.sub(repl, value)


@dataclass(frozen=True)
class PlatformConfig:
    """A resolved review-platform connection."""

    platform: str  # "gitlab" (github planned)
    url: str
    token: str


def invalidate_config_cache() -> None:
    """Clear the cached config (used by tests and the CLI after edits)."""
    load_config.cache_clear()


@lru_cache(maxsize=1)
def load_config() -> PlatformConfig:
    """
    Load and validate the platform configuration.

    Precedence: TOML file values (with ${ENV} expansion) win; otherwise fall back
    to environment variables. Raises ValueError with an actionable message if no
    usable configuration is found.
    """
    # Auto-load the .env sitting next to the config so ${VAR} works out of the box.
    _load_env_file(config_dir() / ".env")

    data: dict = {}
    path = config_path()
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    gitlab = data.get("gitlab", {}) if isinstance(data, dict) else {}

    url = gitlab.get("url") or os.environ.get("MYOPIC_GITLAB_URL") or os.environ.get("GITLAB_URL")
    token = gitlab.get("token") or os.environ.get("MYOPIC_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")

    if not url or not token:
        raise ValueError(
            "myopic is not configured. Set a GitLab URL and token via "
            f"{config_path()} or the GITLAB_URL / GITLAB_TOKEN environment "
            "variables. Run `myopic init` to create a config template."
        )

    return PlatformConfig(
        platform="gitlab",
        url=_expand(url).rstrip("/"),
        token=_expand(token),
    )
