"""
myopic MCP server — FastMCP entry point.

Tools are registered here with full docstrings. The first line of every
docstring is self-contained — non-Claude clients show only that line.

Start with:
  python -m myopic.server
  uvx myopic
"""

from mcp.server.fastmcp import FastMCP

from myopic.tools.changed_files import mr_changed_files as _mr_changed_files
from myopic.tools.code_search import code_search as _code_search
from myopic.tools.dependency_impact import dependency_impact as _dependency_impact
from myopic.tools.diff_lines import mr_diff_lines as _mr_diff_lines
from myopic.tools.diff_sections import mr_diff_sections as _mr_diff_sections
from myopic.tools.index_repo import index_repo as _index_repo
from myopic.tools.review_context import mr_review_context as _mr_review_context
from myopic.tools.review_status import mr_review_status as _mr_review_status
from myopic.tools.trace_call_chain import trace_call_chain as _trace_call_chain

mcp = FastMCP(
    "myopic",
    instructions="""
myopic is a code-review companion for merge requests. The name is ironic — it's
anything but nearsighted. Its goal is to review a change against the whole
codebase, not just the diff in front of it.

Today (alpha) it gives you precise, structured access to a merge request so you
can review it well:
1. mr_review_status(url) — see where a review stands: metadata, every discussion
   thread, and what's resolved vs still open. Start here to orient.
2. mr_changed_files(url) — a content-free manifest of the changed files (paths,
   stats, noise flags). ALWAYS fits, no matter how large the MR. Call this before
   the diff on any big/unknown review, then fetch diffs a batch at a time.
3. mr_diff_sections(url) — the diff grouped by enclosing function/class instead
   of raw hunks. Also token-budgeted (truncated/omitted_files/skipped_files).
   Prefer this over mr_diff_lines on a large MR (many files or a big diff) to
   avoid overflowing the context window while keeping every changed line.
4. mr_diff_lines(url) — fetch the diff as structured, line-numbered hunks. Use
   this to read exactly what changed and to get the precise new-file line numbers
   needed to anchor inline comments. It is token-budgeted: on a big MR it returns
   a bounded page and lists the rest under "omitted_files" (with "truncated":true)
   instead of failing — refetch those via files_filter. Noise (lockfiles,
   generated, binary) is listed under "skipped_files", not expanded. Passing
   files_filter or lines_filter is a targeted fetch that bypasses both guards.

Reviewing against the whole codebase (needs a LOCAL clone of the repo):
5. dependency_impact(symbol, root) — who uses this symbol? The blast radius of a
   change. Run it on the functions/classes touched by the diff before approving.
6. trace_call_chain(symbol, root) — the caller/callee graph of a symbol. Reason
   about ripple effects. These are the highest-value review signal — a change is
   only as safe as what depends on it.

Semantic search — optional, needs `pip install myopic[semantic]` + a local Ollama
server (MYOPIC_OLLAMA_URL, default http://localhost:11434):
7. index_repo(root) — build or refresh the semantic index for a repo. Run once
   per clone; re-run after large changes. Returns {indexed_chunks, files, skipped}.
8. code_search(query, root) — hybrid vector + full-text search over an indexed
   repo. Use to find patterns, conventions, or examples before reviewing a new
   implementation.
9. mr_review_context(url, root) — the graph-first fusion tool. Extracts the top
   changed symbols from the MR diff, runs dependency_impact on each (always,
   no extra needed), and — if myopic[semantic] is installed and the repo is
   indexed — enriches each symbol with related_patterns from a semantic search.
   A structure-only result (semantic_available: false) is a fully valid, complete
   response; semantic context is purely additive.

Recommended flow for any non-trivial MR:
  1. mr_changed_files(url) — see the shape.
  2. mr_diff_sections(url) (large MRs) or mr_diff_lines(url, files_filter=[...batch]) — read the change, token-safe.
  3. For each risky changed symbol: dependency_impact / trace_call_chain against a
     local clone — review the change against everything that depends on it.
  4. (Optional, with myopic[semantic]) mr_review_context(url, root) for a combined
     graph + semantic context snapshot in one call.

If a tool returns an "error" about configuration, the user has not set up a
token yet. Tell them to run `myopic init` in their terminal — do not attempt to
configure it yourself.

Both GitLab merge requests and GitHub pull requests are supported — pass either
URL and the right backend is chosen automatically. GitLab needs GITLAB_TOKEN;
GitHub needs GITHUB_TOKEN.
""",
)


@mcp.tool()
def mr_changed_files(url: str) -> dict:
    """List the files changed in a merge request with stats — no diff content.

    The cheap entry point for large reviews: the payload stays small no matter how
    big the MR is, so it never overflows the context window. Each file reports
    additions/deletions, new/deleted/renamed flags, and a reviewability flag
    (lockfiles, generated code, binary assets, and enormous single-file changes
    are marked reviewable=false with a skip_reason). Files are ordered
    reviewable-first then largest-change-first, so you can batch the highest-value
    files straight into mr_diff_lines(url, files_filter=[...]).

    Args:
        url: Full GitLab merge request URL.

    Returns:
        {mr_number, title, author, branches, commits, diff_shas,
         files[{file_path, additions, deletions, reviewable, skip_reason, ...}],
         stats{total_files, reviewable_files, skipped_files, ...}}
    """
    return _mr_changed_files(url=url)


@mcp.tool()
def mr_diff_sections(
    url: str,
    include_context_lines: bool = False,
    files_filter: list[str] | None = None,
    max_chars: int = 80000,
    skip_noise: bool = True,
) -> dict:
    """
    Fetch a merge request's diff grouped by enclosing function/class, not raw hunks.

    AST-aware for new files (full tree-sitter chunking) and hunk-context-aware
    for modified files (declaration pattern + hunk-header hint). All changed
    lines (add/del) are always included — nothing dropped, only the framing
    changes. Prefer this over mr_diff_lines on a large MR (many files or a big
    diff) since grouping by symbol keeps the payload small without truncating
    mid-function.

    Token-safe by construction, same guarantees as mr_diff_lines: on a large MR
    it returns a bounded page of files and lists the rest under "omitted_files"
    with "truncated": true. Noise files (lockfiles, generated, binary) are
    listed under "skipped_files", not expanded. For unknown-size MRs, call
    mr_changed_files first, then batch files_filter here.

    Args:
        url: Full GitLab merge request URL.
        include_context_lines: Include unchanged surrounding lines in each
                      section. Default False keeps the payload small.
        files_filter: Optional list of file-path fragments to include. Passing
                      this is a TARGETED fetch — noise-skip and the budget are
                      disabled so you get exactly the files you ask for.
        max_chars:    Token-safety budget for the returned diff body (default
                      80000). Ignored for targeted fetches.
        skip_noise:   Keep lockfiles/generated/binary out of the body (listed
                      under skipped_files). Default True. Ignored when filtering.

    Returns:
        {mr_number, title, author, branches, description, commits, diff_shas,
         files[{file_path, language, new_file, deleted_file, additions,
         deletions, sections[{symbol, symbol_type, start_line, end_line,
         changes}]}], truncated, omitted_files, skipped_files, stats}
    """
    return _mr_diff_sections(
        url=url,
        include_context_lines=include_context_lines,
        files_filter=files_filter,
        max_chars=max_chars,
        skip_noise=skip_noise,
    )


@mcp.tool()
def mr_diff_lines(
    url: str,
    files_filter: list[str] | None = None,
    lines_filter: dict[str, list[int]] | None = None,
    max_chars: int = 80000,
    skip_noise: bool = True,
) -> dict:
    """
    Fetch a merge request's diff as structured, line-numbered hunks.

    Pure data, no LLM. Returns exact file paths, old/new line numbers, and diff
    content — everything needed to read a change precisely and to compute the
    diff positions required for inline comments.

    Token-safe by construction: on a large MR it returns a bounded page of files
    and lists the rest under "omitted_files" with "truncated": true (never an
    oversized payload). Noise files (lockfiles, generated, binary) are listed
    under "skipped_files", not expanded. For unknown-size MRs, call
    mr_changed_files first, then batch files_filter here.

    Args:
        url:          Full GitLab merge request URL.
        files_filter: Optional list of file-path fragments to include. Passing
                      this is a TARGETED fetch — noise-skip and the budget are
                      disabled so you get exactly the files you ask for.
        lines_filter: Optional map of filename-fragment -> target new-file line
                      numbers; returns compact line_mappings instead of full hunks.
        max_chars:    Token-safety budget for the returned diff body (default
                      80000). Ignored for targeted fetches.
        skip_noise:   Keep lockfiles/generated/binary out of the body (listed
                      under skipped_files). Default True. Ignored when filtering.

    Returns:
        {mr_number, ..., diff_shas, files[...], truncated, omitted_files,
         skipped_files, stats}
    """
    return _mr_diff_lines(
        url=url,
        files_filter=files_filter,
        lines_filter=lines_filter,
        max_chars=max_chars,
        skip_noise=skip_noise,
    )


@mcp.tool()
def mr_review_status(url: str) -> dict:
    """
    Get a merge request's review status: metadata + discussions + resolution.

    Pure data, no LLM. Collapses several platform API calls into one snapshot of
    where the review stands — every discussion thread, what's resolved vs open,
    general comments, and a lightweight file-change summary. Start here to orient
    before diving into the diff.

    Args:
        url: Full GitLab merge request URL.

    Returns:
        {mr_number, title, author, branches, state, merge_status, commits,
         general_comments, discussions[...], summary{resolved, unresolved, ...},
         files_changed[...], stats}
    """
    return _mr_review_status(url=url)


@mcp.tool()
def dependency_impact(
    symbol: str,
    root: str,
    file_glob: str | None = None,
    whole_word: bool = True,
    max_results: int = 50,
) -> dict:
    """Find everywhere a symbol is used in a checked-out repo (the blast radius).

    The highest-value review signal: before you approve a change to a function,
    class, or constant, see who depends on it. Uses ripgrep for fast candidate
    finding, then classifies each usage via tree-sitter AST as call / import /
    definition / type_reference. Filesystem-based — point it at a LOCAL clone of
    the repo the MR belongs to, not the MR URL.

    Args:
        symbol:      Function/class/variable name to trace.
        root:        Absolute path to the local repository clone.
        file_glob:   Optional glob to narrow the search (e.g. "*.py", "src/**/*.ts").
        whole_word:  Match whole words only (default True).
        max_results: Cap on references returned (default 50).

    Returns:
        {symbol, root, total_references, references[{file_path, line, context,
         usage_type}], by_type{...}}
    """
    return _dependency_impact(
        symbol=symbol, root=root, file_glob=file_glob,
        whole_word=whole_word, max_results=max_results,
    )


@mcp.tool()
def trace_call_chain(
    symbol: str,
    root: str,
    language: str | None = None,
    max_depth: int = 1,
) -> dict:
    """Trace a function's callers and callees across a checked-out repo (AST).

    Complements dependency_impact: where dependency_impact lists every reference,
    this builds the directed call graph — where the symbol is defined, what it
    calls, and what calls it — so you can reason about a change's ripple effects.
    Tree-sitter-based; point it at a LOCAL clone of the repo.

    Args:
        symbol:    Function or class name to trace.
        root:      Absolute path to the local repository clone.
        language:  Restrict to one language; auto-detects if omitted.
        max_depth: Levels of callers/callees to follow (default 1).

    Returns:
        {symbol, definition{file_path, line, type}, callees[...], callers[...],
         stats{files_scanned, parse_errors}}
    """
    return _trace_call_chain(
        symbol=symbol, root=root, language=language, max_depth=max_depth,
    )


@mcp.tool()
def index_repo(root: str) -> dict:
    """Build or refresh a semantic search index for a local repository.

    Walks the repo, chunks every supported-language file by AST boundaries,
    embeds the chunks via a local Ollama server, and stores them in a per-repo
    LanceDB table. Run once per clone; re-run after substantial changes. Requires
    the myopic[semantic] extra and Ollama running at MYOPIC_OLLAMA_URL (default
    http://localhost:11434) with the embedding model pulled (MYOPIC_EMBED_MODEL).

    Args:
        root: Absolute path to the repository to index.

    Returns:
        {indexed_chunks, files, skipped} on success, or {"error": "..."} if the
        semantic extra is not installed or Ollama is unreachable.
    """
    return _index_repo(root=root)


@mcp.tool()
def code_search(query: str, root: str, k: int = 8) -> dict:
    """Hybrid semantic + full-text search over an indexed local repository.

    Embeds the query via Ollama, then runs a combined vector + FTS search with
    RRF reranking against the LanceDB index built by index_repo. Use this to
    find existing patterns, conventions, or examples in the codebase before
    reviewing a new implementation. Requires myopic[semantic] and index_repo to
    have been run first.

    Args:
        query: Natural language or code snippet describing what to find.
        root:  Absolute path to the repository (must have been indexed first).
        k:     Maximum number of results to return (default 8).

    Returns:
        {query, root, results[{file_path, symbol, symbol_type, start_line,
         end_line, text, score?}]} or {"error": "..."} on failure.
    """
    return _code_search(query=query, root=root, k=k)


@mcp.tool()
def mr_review_context(url: str, root: str, max_symbols: int = 8) -> dict:
    """Graph-first review context: dependency impact per changed symbol, plus optional semantic enrichment.

    Extracts the top-N most-frequent identifiers from the MR diff, then for each:
    1. Runs dependency_impact(symbol, root) — always, no optional extras needed.
    2. If myopic[semantic] is installed AND the repo has been indexed via
       index_repo(root), enriches each symbol with related_patterns from a hybrid
       semantic search against the codebase.

    The semantic layer is purely additive: a result with semantic_available=false
    is complete and actionable — dependency impact already covers the blast radius.
    Use this as a single-call alternative to running dependency_impact separately
    for each changed symbol.

    Args:
        url:         Full GitLab merge request URL.
        root:        Absolute path to the local repository clone.
        max_symbols: Maximum changed symbols to analyze (default 8).

    Returns:
        {mr_number, symbols[{symbol, impact, related_patterns?}],
         semantic_available} or {"error": "..."} on review-open failure.
    """
    return _mr_review_context(url=url, root=root, max_symbols=max_symbols)


def main() -> None:
    """Entry point for `python -m myopic.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
