"""
mr_review_context — graph-first review context: dependency impact per changed
symbol, optionally enriched with semantic search when myopic[semantic] is
installed and the repo has been indexed.

Changed symbols come from the SAME AST/section resolution as mr_diff_sections —
the actual functions/classes the diff touched — not a raw identifier-frequency
count. That keeps stopwords and common tokens (`the`, `number`, `styles`) out of
the analysis; dependency_impact runs only on real declarations.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from myopic.diff import EXT_TO_LANG, parse_hunks
from myopic.platforms.base import open_review
from myopic.tools.dependency_impact import dependency_impact
from myopic.tools.diff_sections import changed_symbols

# ---------------------------------------------------------------------------
# Fallback identifier extraction (only used when AST resolves no changed symbol,
# e.g. a diff of a language without declaration patterns). Scans ADDED lines
# only — context lines are unchanged surrounding code and were the main source
# of noise in the old frequency heuristic.
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_MIN_TOKEN_LEN = 3

_STOPWORDS: frozenset[str] = frozenset({
    "if", "else", "elif", "for", "while", "do", "switch", "case", "break",
    "continue", "return", "yield", "try", "catch", "finally", "throw", "raises",
    "with", "as", "in", "not", "and", "or", "is", "new", "delete", "typeof",
    "instanceof", "await", "async",
    "def", "class", "function", "const", "let", "var", "val", "fun",
    "fn", "pub", "priv", "private", "public", "protected", "static", "final",
    "abstract", "override", "open", "sealed", "data", "enum", "interface",
    "struct", "impl", "trait", "mod", "use", "import", "from", "export",
    "package",
    "void", "int", "long", "float", "double", "bool", "boolean", "str", "string",
    "true", "false", "null", "None", "nil", "undefined", "NaN", "Infinity",
    "this", "self", "super",
    "it", "to", "of", "at", "be", "by", "on",
})


def _extract_added_identifiers(patch_text: str) -> Counter:
    """Candidate symbol names from ADDED lines only (fallback path)."""
    counts: Counter = Counter()
    for hunk in parse_hunks(patch_text):
        for line in hunk["lines"]:
            if line["type"] != "add":
                continue
            for m in _IDENT_RE.finditer(line["content"]):
                token = m.group(1)
                if len(token) >= _MIN_TOKEN_LEN and token not in _STOPWORDS:
                    counts[token] += 1
    return counts


def _select_changed_symbols(diff_set, max_symbols: int) -> tuple[list[str], dict[str, str], str]:
    """Return (top_symbol_names, symbol_type_by_name, source).

    Primary: real changed declarations (AST/section resolution), weighted by
    changed-line count. Fallback: added-line identifier frequency.
    """
    weight: Counter = Counter()
    types: dict[str, str] = {}
    for fd in diff_set.files:
        if not fd.patch:
            continue
        language = EXT_TO_LANG.get(Path(fd.file_path).suffix.lower())
        for cs in changed_symbols(fd.patch, language, fd.new_file):
            weight[cs["symbol"]] += cs["changed_lines"]
            types.setdefault(cs["symbol"], cs["symbol_type"])

    if weight:
        top = [name for name, _ in weight.most_common(max_symbols)]
        return top, types, "ast"

    # Fallback — no resolvable declarations (e.g. config/data-only diff).
    fallback: Counter = Counter()
    for fd in diff_set.files:
        if fd.patch:
            fallback.update(_extract_added_identifiers(fd.patch))
    top = [name for name, _ in fallback.most_common(max_symbols)]
    return top, {}, "identifier-fallback"


def mr_review_context(url: str, root: str, max_symbols: int = 8) -> dict:
    """Build graph-first review context for a merge/pull request.

    For each of the top changed *declarations* in the diff (real functions/
    classes, ranked by how much they changed):
    1. Runs dependency_impact(symbol, root) unconditionally — the blast radius.
    2. If myopic[semantic] is installed AND the repo is indexed, enriches each
       symbol with related_patterns from a hybrid semantic search.

    Semantic enrichment is additive: without the extra (or an index) the result
    is still complete. When an index exists, index_status reports its freshness
    so the caller can decide whether to refresh via index_repo first.

    Args:
        url:         Full merge/pull request URL.
        root:        Absolute path to the local repository clone.
        max_symbols: Cap on changed symbols to analyze (default 8).

    Returns:
        {mr_number, symbols[{symbol, symbol_type?, impact, related_patterns?}],
         symbol_source, semantic_available, index_status?, next?}
        or {"error": "..."} on review-open failure.
    """
    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    meta = review.metadata()
    diff_set = review.diffs()

    # --- Step 1: real changed symbols (with add-line fallback) ---------------
    top_symbols, symbol_types, symbol_source = _select_changed_symbols(diff_set, max_symbols)

    # --- Step 2: probe semantic availability + index freshness ---------------
    semantic_available = False
    index_state: dict | None = None
    _semantic_index = None
    _embed_texts = None

    try:
        from myopic.embeddings import embed_texts as _embed_texts_fn
        from myopic.semantic.indexer import index_status as _index_status
        from myopic.semantic.store import CodeIndex

        idx = CodeIndex.connect(root)
        if idx.has_table():
            semantic_available = True
            _semantic_index = idx
            _embed_texts = _embed_texts_fn
        try:
            index_state = _index_status(root)
        except Exception:
            index_state = None
    except (ImportError, RuntimeError):
        pass

    # --- Step 3: per-symbol analysis -----------------------------------------
    symbols_out = []
    for sym in top_symbols:
        entry: dict = {"symbol": sym, "impact": dependency_impact(sym, root)}
        if sym in symbol_types:
            entry["symbol_type"] = symbol_types[sym]

        if semantic_available and _semantic_index is not None and _embed_texts is not None:
            context_hint = _first_context_for(diff_set, sym)
            query = f"{sym} {context_hint}".strip()
            try:
                query_vector = _embed_texts([query])[0]
                raw = _semantic_index.hybrid_search(query, query_vector, k=5)
                related = []
                for row in raw:
                    r: dict = {
                        "file_path": row.get("file_path"),
                        "symbol": row.get("symbol"),
                        "symbol_type": row.get("symbol_type"),
                        "start_line": row.get("start_line"),
                        "end_line": row.get("end_line"),
                        "text": row.get("text"),
                    }
                    if "_relevance_score" in row:
                        r["score"] = row["_relevance_score"]
                    related.append(r)
                entry["related_patterns"] = related
            except RuntimeError:
                pass

        symbols_out.append(entry)

    result = {
        "mr_number": meta.number,
        "symbols": symbols_out,
        "symbol_source": symbol_source,
        "semantic_available": semantic_available,
    }
    if index_state is not None:
        result["index_status"] = index_state
        state = index_state.get("state")
        if state in ("stale", "model_mismatch", "unknown"):
            behind = index_state.get("commits_behind")
            behind_txt = f" ({behind} commits behind)" if behind else ""
            result["next"] = (
                f"Semantic index is {state}{behind_txt}. Its related_patterns may be "
                f"out of date — consider index_repo(root={root!r}) to refresh "
                "(incremental, only changed files are re-embedded)."
            )
    return result


def _first_context_for(diff_set, sym: str) -> str:
    """A short changed-line snippet mentioning sym, to sharpen the semantic query."""
    for fd in diff_set.files:
        if not fd.patch:
            continue
        for hunk in parse_hunks(fd.patch):
            for line in hunk["lines"]:
                if line["type"] != "del" and sym in line["content"]:
                    return line["content"].strip()[:200]
    return ""
