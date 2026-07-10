"""
code_search — hybrid semantic + full-text search over an indexed local repo.
"""

from __future__ import annotations


def code_search(query: str, root: str, k: int = 8) -> dict:
    """Search an indexed repository for code matching a semantic query.

    Performs hybrid vector + full-text search with RRF reranking using a
    LanceDB index previously built by index_repo. Run index_repo(root) first.

    Requires a running Ollama (the semantic layer is built in; run `myopic doctor`
    to set it up) for query embedding.

    Args:
        query: Natural language or code snippet describing what to find.
        root:  Absolute path to the repository (must have been indexed first).
        k:     Maximum number of results to return (default 8).

    Returns:
        {query, root, results[{file_path, symbol, symbol_type, start_line,
        end_line, text, score?}]} or {"error": "..."} on failure.
    """
    try:
        from myopic.embeddings import embed_texts
        from myopic.semantic.store import CodeIndex
    except ImportError:
        return {
            "error": (
                "the semantic layer is bundled — reinstall myopic if this import fails"
            )
        }

    try:
        index = CodeIndex.connect(root)
    except RuntimeError as exc:
        return {"error": str(exc)}

    if not index.has_table():
        return {
            "error": (
                f"Repo not indexed yet. Run index_repo(root={root!r}) first."
            )
        }

    try:
        query_vector = embed_texts([query])[0]
        raw_results = index.hybrid_search(query, query_vector, k=k)
    except RuntimeError as exc:
        return {"error": str(exc)}

    # Best-effort freshness: results come from whatever commit was last indexed.
    index_state = None
    try:
        from myopic.semantic.indexer import index_status as _index_status
        index_state = _index_status(root)
    except Exception:
        index_state = None

    results = []
    for row in raw_results:
        entry: dict = {
            "file_path": row.get("file_path"),
            "symbol": row.get("symbol"),
            "symbol_type": row.get("symbol_type"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "text": row.get("text"),
        }
        # Include relevance score if LanceDB returned one.
        if "_relevance_score" in row:
            entry["score"] = row["_relevance_score"]
        results.append(entry)

    out = {"query": query, "root": root, "results": results}
    if index_state is not None:
        out["index_status"] = index_state
        if index_state.get("state") in ("stale", "model_mismatch"):
            out["next"] = (
                f"Index is {index_state['state']} — results may be out of date; "
                f"run index_repo(root={root!r}) to refresh."
            )
    return out
