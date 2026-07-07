"""
index_repo — build/refresh a local semantic search index for a repository.
"""

from __future__ import annotations


def index_repo(root: str) -> dict:
    """Index a local repo's code into a LanceDB store for semantic search.

    Walks the repository, chunks every supported-language file by AST
    boundaries, embeds the chunks via a local Ollama server, and writes them
    to a per-repo LanceDB table. Subsequent code_search calls query this index.

    Requires the myopic[semantic] extra (lancedb + httpx) and a running Ollama
    instance. Override the model with MYOPIC_EMBED_MODEL and the server URL
    with MYOPIC_OLLAMA_URL.

    Args:
        root: Absolute path to the repository to index.

    Returns:
        {indexed_chunks, files, skipped} on success, or {"error": "..."} if
        the semantic extra is not installed or indexing fails.
    """
    try:
        from myopic.semantic.indexer import index_repo as _index_repo
    except ImportError:
        return {
            "error": (
                "semantic search needs the optional extra — "
                "install with: pip install myopic[semantic]"
            )
        }
    try:
        return _index_repo(root)
    except RuntimeError as exc:
        return {"error": str(exc)}
