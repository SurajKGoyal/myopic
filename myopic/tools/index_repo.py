"""
index_repo / index_status — build, incrementally refresh, and inspect a
local semantic search index for a repository.
"""

from __future__ import annotations

_EXTRA_MISSING = (
    "semantic search needs the optional extra — install with: pip install myopic[semantic]"
)


def index_repo(root: str, force: bool = False) -> dict:
    """Index a local repo's code into a LanceDB store for semantic search.

    Walks the repository, chunks every supported-language file by AST boundaries,
    embeds the chunks via a local Ollama server, and writes them to a per-repo
    LanceDB table. After the first build this is INCREMENTAL: only files whose
    content changed since the last run are re-embedded (seconds, not minutes). A
    changed embedding model or force=True triggers a full rebuild.

    Requires the myopic[semantic] extra (lancedb + httpx) and a running Ollama
    instance. Override the model with MYOPIC_EMBED_MODEL and the server URL with
    MYOPIC_OLLAMA_URL.

    Args:
        root:  Absolute path to the repository to index.
        force: Rebuild the whole index even if an up-to-date one exists.

    Returns:
        {mode, indexed_chunks, files, skipped, changed_files, deleted_files,
         git_sha, model} on success, or {"error": "..."} on failure.
    """
    try:
        from myopic.semantic.indexer import index_repo as _index_repo
    except ImportError:
        return {"error": _EXTRA_MISSING}
    try:
        return _index_repo(root, force=force)
    except RuntimeError as exc:
        return {"error": str(exc)}


def index_status(root: str) -> dict:
    """Report whether a repo's semantic index is fresh, stale, or absent.

    Freshness is keyed to the git commit the index was built from: if HEAD has
    moved on, the index is "stale" and reports how many commits behind. Use this
    before relying on semantic results to decide whether to index_repo first.

    Args:
        root: Absolute path to the repository.

    Returns:
        {state: absent|fresh|stale|model_mismatch|unknown, root, chunks?,
         indexed_at?, indexed_sha?, current_sha?, commits_behind?, reason?}
        or {"error": "..."} if the semantic extra is not installed.
    """
    try:
        from myopic.semantic.indexer import index_status as _index_status
    except ImportError:
        return {"error": _EXTRA_MISSING}
    try:
        return _index_status(root)
    except RuntimeError as exc:
        return {"error": str(exc)}
