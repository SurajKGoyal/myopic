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


def embed_model() -> str:
    """Ollama embedding model name (override via MYOPIC_EMBED_MODEL)."""
    return os.environ.get("MYOPIC_EMBED_MODEL", "unclemusclez/jina-embeddings-v2-base-code")


def ollama_url() -> str:
    """Ollama base URL (override via MYOPIC_OLLAMA_URL)."""
    return os.environ.get("MYOPIC_OLLAMA_URL", "http://localhost:11434").rstrip("/")


def auto_pull() -> bool:
    """Whether to auto-pull a missing embedding model (opt-in via MYOPIC_AUTO_PULL)."""
    return os.environ.get("MYOPIC_AUTO_PULL", "").strip().lower() in ("1", "true", "yes", "on")


def auto_index() -> bool:
    """Whether mr_review_context auto-builds/refreshes the index (default ON).

    The semantic layer is built in, so we make
    it just work — index on first review, refresh when stale — with no manual
    index_repo. Disable with MYOPIC_AUTO_INDEX=0 (e.g. to avoid a slow first build
    inside a review call; the graph pass works regardless).
    """
    return os.environ.get("MYOPIC_AUTO_INDEX", "").strip().lower() not in ("0", "false", "no", "off")


def index_dir() -> Path:
    """Directory where semantic-search LanceDB indexes are stored."""
    return config_dir() / "index"


def invalidate_config_cache() -> None:
    """Clear the cached config (used by tests and the CLI after edits)."""
    load_config.cache_clear()


@lru_cache(maxsize=None)
def load_config(platform: str = "gitlab") -> PlatformConfig:
    """
    Load and validate the config for a review platform ("gitlab" or "github").

    The TOML section `[<platform>]` wins (with ${ENV} expansion); environment
    variables are the fallback. GitLab needs a URL + token; GitHub needs only a
    token (the URL is optional — it's just the GitHub Enterprise base host, and
    public github.com is the default). Raises ValueError with an actionable
    message when the required values are missing.
    """
    # Auto-load the .env sitting next to the config so ${VAR} works out of the box.
    _load_env_file(config_dir() / ".env")

    data: dict = {}
    path = config_path()
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = data.get(platform, {}) if isinstance(data, dict) else {}

    if platform == "gitlab":
        url = section.get("url") or os.environ.get("MYOPIC_GITLAB_URL") or os.environ.get("GITLAB_URL")
        token = section.get("token") or os.environ.get("MYOPIC_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")
        if not url or not token:
            raise ValueError(
                "myopic is not configured for GitLab. Set a URL and token via "
                f"{config_path()} [gitlab] or the GITLAB_URL / GITLAB_TOKEN "
                "environment variables. Run `myopic init`."
            )
        return PlatformConfig("gitlab", _expand(url).rstrip("/"), _expand(token))

    if platform == "github":
        # URL is optional — only GitHub Enterprise needs a base host; the adapter
        # defaults to public github.com / api.github.com when it's empty.
        url = (
            section.get("url")
            or os.environ.get("MYOPIC_GITHUB_URL")
            or os.environ.get("GITHUB_URL")
            or ""
        )
        token = section.get("token") or os.environ.get("MYOPIC_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError(
                "myopic is not configured for GitHub. Set a token via "
                f"{config_path()} [github] or the GITHUB_TOKEN environment "
                "variable (a PAT with repo / pull-request read scope)."
            )
        return PlatformConfig("github", _expand(url).rstrip("/") if url else "", _expand(token))

    raise ValueError(f"Unknown platform: {platform!r} (expected 'gitlab' or 'github').")
