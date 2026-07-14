"""
Tiny git helpers for index freshness.

The semantic index is keyed to the commit it was built from, so the review
tools can tell "this index is 12 commits behind HEAD" instead of silently
serving stale results. All helpers are best-effort: a non-git directory, a
missing git binary, or any git error resolves to a safe None/False rather than
raising — freshness is an optimization, never a hard dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(root: str, args: list[str]) -> str | None:
    """Run `git -C root <args>` and return stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def sha_of(root: str, ref: str) -> str | None:
    """Resolve a ref to its full commit SHA, or None if it doesn't exist."""
    return _run(root, ["rev-parse", "--verify", "--quiet", ref]) or None


def remote_url(root: str) -> str | None:
    """The `origin` remote URL, normalized, or None if there's no origin.

    Two separate clones of the same repository share this — unlike the
    git-common-dir key, which is per-clone — so it identifies "the same project"
    across clones for de-duplicating stale indexes. Normalized (lowercased, `.git`
    and trailing slash stripped, scp-style `git@host:path` → `host/path`) so
    equivalent URLs compare equal.
    """
    raw = _run(root, ["remote", "get-url", "origin"])
    if not raw:
        return None
    u = raw.strip().lower()
    if u.startswith("git@") and ":" in u:  # scp-style → host/path
        u = u[len("git@"):].replace(":", "/", 1)
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if "@" in u.split("/", 1)[0]:  # strip user@ credentials in the host segment
        u = u.split("@", 1)[1]
    return u[:-4] if u.endswith(".git") else u.rstrip("/")


def head_sha(root: str) -> str | None:
    """Full SHA of HEAD, or None if root isn't a git repo / git is unavailable."""
    return sha_of(root, "HEAD")


def default_branch_ref(root: str) -> str | None:
    """The ref name of the repo's main line — origin's default branch if known,
    else a local main/master. This is what a code index's freshness is measured
    against, so reviewing a feature branch doesn't make the index look stale.
    """
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if _run(root, ["rev-parse", "--verify", "--quiet", ref]) is not None:
            return ref
    return None


def default_branch_sha(root: str) -> str | None:
    """SHA of the repo's main line (see default_branch_ref), or None."""
    ref = default_branch_ref(root)
    return sha_of(root, ref) if ref else None


def is_dirty(root: str) -> bool:
    """True if the working tree has uncommitted changes. False if unknown."""
    out = _run(root, ["status", "--porcelain"])
    return bool(out)


def commits_behind(root: str, old_sha: str, ref: str = "HEAD") -> int | None:
    """How many commits `ref` is ahead of old_sha (old_sha..ref); ref defaults
    to HEAD but is typically the main line (e.g. origin/main).

    Returns None if old_sha is unknown to the repo (e.g. history was rewritten
    or the commit isn't present), so callers can distinguish "can't tell" from
    "zero behind".
    """
    if not old_sha:
        return None
    out = _run(root, ["rev-list", "--count", f"{old_sha}..{ref}"])
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def commit_present(root: str, sha: str) -> bool:
    """True if `sha` is an object present in the clone at `root`."""
    if not sha:
        return False
    return _run(root, ["cat-file", "-e", f"{sha}^{{commit}}"]) is not None


def fetch_ref(root: str, ref: str) -> bool:
    """`git fetch origin <ref>` — pull a branch so its commits become local."""
    return _run(root, ["fetch", "origin", ref]) is not None


def add_worktree(root: str, path: str, ref: str) -> bool:
    """Add a detached worktree at `ref` under `path`. No new branch is created."""
    return _run(root, ["worktree", "add", "--detach", str(path), ref]) is not None


def common_dir(root: str) -> str | None:
    """Absolute path to the repository's shared git dir (`--git-common-dir`).

    A clone and all of its linked worktrees resolve to the SAME value, which makes
    it a stable per-repository key: the semantic index built from the main clone
    and one built from a worktree land in the same table. Returns None if `root`
    isn't a git repo (callers fall back to the path).
    """
    out = _run(root, ["rev-parse", "--git-common-dir"])
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = Path(root) / p
    try:
        return str(p.resolve())
    except OSError:
        return None


def short(sha: str | None, length: int = 8) -> str | None:
    """Short form of a SHA for display, or None passthrough."""
    return sha[:length] if sha else None


def is_git_repo(root: str) -> bool:
    """True if root is inside a git work tree."""
    return _run(root, ["rev-parse", "--is-inside-work-tree"]) == "true"


def resolve_root(root: str) -> str:
    """Absolute path of root (git helpers accept either abs or rel)."""
    return str(Path(root).resolve())
