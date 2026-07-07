"""
mr_review_context — graph-first review context: dependency impact per changed
symbol, optionally enriched with semantic search when myopic[semantic] is
installed and the repo has been indexed.
"""

from __future__ import annotations

import re
from collections import Counter

from myopic.diff import parse_hunks
from myopic.platforms.base import open_review
from myopic.tools.dependency_impact import dependency_impact

# ---------------------------------------------------------------------------
# Identifier extraction helpers
# ---------------------------------------------------------------------------

# Broad identifier regex — catches Python, Java, JS/TS, Go, Kotlin, Rust names.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")

# Language keywords and noise tokens to exclude from the changed-symbol heuristic.
# This is intentionally a broad superset across languages — false negatives
# (excluding a real symbol) are much cheaper than false positives (running
# dependency_impact on "if" or "null").
_STOPWORDS: frozenset[str] = frozenset({
    # Universal control flow / literals
    "if", "else", "elif", "for", "while", "do", "switch", "case", "break",
    "continue", "return", "yield", "try", "catch", "finally", "throw", "raises",
    "with", "as", "in", "not", "and", "or", "is", "new", "delete", "typeof",
    "instanceof", "await", "async",
    # Declaration keywords
    "def", "class", "function", "const", "let", "var", "val", "var", "fun",
    "fn", "pub", "priv", "private", "public", "protected", "static", "final",
    "abstract", "override", "open", "sealed", "data", "enum", "interface",
    "struct", "impl", "trait", "mod", "use", "import", "from", "export",
    "package",
    # Types / builtins
    "void", "int", "long", "float", "double", "bool", "boolean", "str", "string",
    "true", "false", "null", "None", "nil", "undefined", "NaN", "Infinity",
    "this", "self", "super",
    # Common noise (very short tokens are already filtered by length, but cover
    # some two-letter ones that would otherwise slip through)
    "it", "to", "of", "at", "be", "by", "on",
})

_MIN_TOKEN_LEN = 3  # single- and double-char tokens aren't meaningful symbols


def _extract_identifiers(patch_text: str) -> Counter:
    """Extract candidate symbol names from add/context lines of a diff patch.

    Returns a Counter of identifier -> occurrence count across the full patch.
    We scan add and context lines only (del lines are the OLD code — not what
    the MR is introducing). Stopwords and short tokens are excluded.
    """
    counts: Counter = Counter()
    for hunk in parse_hunks(patch_text):
        for line in hunk["lines"]:
            if line["type"] == "del":
                continue
            for m in _IDENT_RE.finditer(line["content"]):
                token = m.group(1)
                if len(token) >= _MIN_TOKEN_LEN and token not in _STOPWORDS:
                    counts[token] += 1
    return counts


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------


def mr_review_context(url: str, root: str, max_symbols: int = 8) -> dict:
    """Build graph-first review context for a merge request.

    For each of the top-N most-frequent identifiers in the diff:
    1. Runs dependency_impact(symbol, root) unconditionally — no optional deps
       needed, always produces a result.
    2. If myopic[semantic] is installed AND the repo has been indexed, enriches
       each symbol with related_patterns from a hybrid semantic search.

    The semantic enrichment is purely additive: when the extra is absent or the
    repo is not yet indexed, the response is still complete and useful — just
    without the related_patterns field on each symbol.

    Args:
        url:         Full GitLab merge request URL.
        root:        Absolute path to the local repository clone.
        max_symbols: Cap on number of changed symbols to analyze (default 8).

    Returns:
        {mr_number, symbols[{symbol, impact, related_patterns?}],
         semantic_available} or {"error": "..."} on review-open failure.
    """
    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    meta = review.metadata()
    diff_set = review.diffs()

    # --- Step 1: collect candidate identifiers across the whole diff ----------
    all_counts: Counter = Counter()
    for fd in diff_set.files:
        if not fd.patch:
            continue
        all_counts.update(_extract_identifiers(fd.patch))

    # Cap at max_symbols BEFORE doing any dependency_impact work.
    top_symbols = [sym for sym, _ in all_counts.most_common(max_symbols)]

    # --- Step 2: probe whether semantic search is available -------------------
    # We try once; the result governs whether we attempt it per symbol.
    semantic_available = False
    _semantic_index = None  # CodeIndex handle, if available and indexed

    try:
        from myopic.embeddings import embed_texts as _embed_texts
        from myopic.semantic.store import CodeIndex

        idx = CodeIndex.connect(root)
        if idx.has_table():
            semantic_available = True
            _semantic_index = idx
    except (ImportError, RuntimeError):
        pass

    # --- Step 3: per-symbol analysis -----------------------------------------
    symbols_out = []
    for sym in top_symbols:
        entry: dict = {
            "symbol": sym,
            "impact": dependency_impact(sym, root),
        }

        if semantic_available and _semantic_index is not None:
            # Build a short query: symbol name + first 200 chars of changed context.
            context_snippets: list[str] = []
            for fd in diff_set.files:
                if not fd.patch:
                    continue
                for hunk in parse_hunks(fd.patch):
                    for line in hunk["lines"]:
                        if line["type"] != "del" and sym in line["content"]:
                            context_snippets.append(line["content"].strip())
                            break
                    if context_snippets:
                        break

            context_hint = " ".join(context_snippets)[:200]
            query = f"{sym} {context_hint}".strip()

            try:
                query_vector = _embed_texts([query])[0]
                raw = _semantic_index.hybrid_search(query, query_vector, k=5)
                related = []
                for row in raw:
                    result_entry: dict = {
                        "file_path": row.get("file_path"),
                        "symbol": row.get("symbol"),
                        "symbol_type": row.get("symbol_type"),
                        "start_line": row.get("start_line"),
                        "end_line": row.get("end_line"),
                        "text": row.get("text"),
                    }
                    if "_relevance_score" in row:
                        result_entry["score"] = row["_relevance_score"]
                    related.append(result_entry)
                entry["related_patterns"] = related
            except RuntimeError:
                # Semantic search failed for this symbol — omit the key entirely.
                pass

        symbols_out.append(entry)

    return {
        "mr_number": meta.number,
        "symbols": symbols_out,
        "semantic_available": semantic_available,
    }
