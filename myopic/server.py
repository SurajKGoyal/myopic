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
from myopic.tools.index_repo import index_status as _index_status
from myopic.tools.post_comments import mr_post_comments as _mr_post_comments
from myopic.tools.review_context import mr_review_context as _mr_review_context
from myopic.tools.review_status import mr_review_status as _mr_review_status
from myopic.tools.trace_call_chain import trace_call_chain as _trace_call_chain
from myopic.tools.verify_review import mr_verify_review as _mr_verify_review

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

ATTRIBUTION: these scan the WHOLE repo, so results include code this MR did not
touch. Call a finding "introduced by this MR" only if it appears in the diff
(verify with mr_diff_lines); otherwise report it as pre-existing. Surfacing
nearby pre-existing issues is useful — mislabeling them as this MR's is not.

Semantic search — optional, needs `pip install myopic[semantic]` + a local Ollama
server (MYOPIC_OLLAMA_URL, default http://localhost:11434):
7. index_repo(root) — build or INCREMENTALLY refresh the semantic index. The
   first run is a full build; after that only changed files are re-embedded, so
   refreshing is cheap. force=True rebuilds everything. Returns {mode,
   indexed_chunks, changed_files, ...}.
8. index_status(root) — is the index fresh, stale, or absent? Freshness is keyed
   to the git commit it was built from ("stale" reports commits_behind). Check
   this before leaning on semantic results; if stale, ASK THE USER whether to
   index_repo(root) first (it's incremental — usually seconds).
9. code_search(query, root) — hybrid vector + full-text search over an indexed
   repo. Use to find patterns, conventions, or examples before reviewing a new
   implementation. Its result carries index_status so you can see staleness.
10. mr_review_context(url, root) — the graph-first fusion tool. Extracts the real
   changed DECLARATIONS from the diff (same AST resolution as mr_diff_sections,
   not a token-frequency guess), runs dependency_impact on each (always, no extra
   needed), and — if myopic[semantic] is installed and the repo is indexed —
   enriches each with related_patterns from a semantic search. A structure-only
   result (semantic_available: false) is fully valid; semantic context is
   additive. It also surfaces index_status — if it reports stale, offer a refresh.
   ROOT MUST HOLD THE MR: it checks whether the MR head is checked out at `root`
   and returns root_status + a "warning" if not. If you see that warning (clone is
   on the target branch, MR code absent), STOP — set up the MR branch first with
   `myopic worktree <url> <repo>` and re-run against the printed path. Otherwise
   graph results reflect code that lacks the MR's changes.

Closing the loop — verify and (on explicit request) comment:
11. mr_verify_review(url) — read-only. For each existing review thread, shows the
    diff changes near the commented line, so you can tell what was addressed vs
    still open without re-reading the whole diff. Use it to re-review after the
    author pushes follow-up commits.
12. mr_post_comments(url, comments) — the ONLY mutating tool. Posts inline
    comments one at a time (queue + exponential backoff, no drafts/bulk-publish).
    Each comment needs file_path, body, and new_line or old_line — get exact line
    numbers from mr_diff_lines first. Only call this when the user explicitly asks
    to post the review; never speculatively.

Recommended flow for any non-trivial MR:
  1. mr_changed_files(url) — see the shape.
  2. mr_diff_sections(url) (large MRs) or mr_diff_lines(url, files_filter=[...batch]) — read the change, token-safe.
  3. For each risky changed symbol: dependency_impact / trace_call_chain against a
     local clone — review the change against everything that depends on it.
  4. (Optional, with myopic[semantic]) mr_review_context(url, root) for a combined
     graph + semantic context snapshot in one call.
  5. If the user asks to post the review: get line numbers via mr_diff_lines, then
     mr_post_comments(url, comments). Later, mr_verify_review(url) to confirm.

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
def index_repo(root: str, force: bool = False) -> dict:
    """Build or incrementally refresh a semantic search index for a local repository.

    Walks the repo, chunks every supported-language file by AST boundaries,
    embeds the chunks via a local Ollama server, and stores them in a per-repo
    LanceDB table. After the first build this is INCREMENTAL: only files whose
    content changed since the last run are re-embedded, so refreshing is cheap —
    run it freely (e.g. when index_status reports "stale"). A changed embedding
    model or force=True does a full rebuild. Requires the myopic[semantic] extra
    and Ollama at MYOPIC_OLLAMA_URL (default http://localhost:11434) with the
    model pulled (MYOPIC_EMBED_MODEL).

    Args:
        root:  Absolute path to the repository to index.
        force: Rebuild the whole index even if an up-to-date one exists.

    Returns:
        {mode, indexed_chunks, files, skipped, changed_files, deleted_files,
         git_sha, model} on success, or {"error": "..."} on failure.
    """
    return _index_repo(root=root, force=force)


@mcp.tool()
def index_status(root: str) -> dict:
    """Report whether a repo's semantic index is fresh, stale, or absent.

    Freshness is keyed to the git commit the index was built from — if HEAD has
    moved on, the index is "stale" and reports how many commits behind. Check
    this before leaning on semantic results (code_search / mr_review_context):
    if state is "stale" or "model_mismatch", offer to index_repo(root) first.

    Args:
        root: Absolute path to the repository.

    Returns:
        {state: absent|fresh|stale|model_mismatch|unknown, root, chunks?,
         indexed_at?, indexed_sha?, current_sha?, commits_behind?, reason?}
        or {"error": "..."} if the semantic extra is not installed.
    """
    return _index_status(root=root)


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
        {mr_number, symbols[{symbol, impact, related_patterns?}], symbol_source,
         semantic_available, root_status?, warning?, index_status?} or
         {"error": "..."}. A "warning" means `root` isn't checked out to the MR —
         set it up with `myopic worktree <url> <repo>` and re-run against its path.
    """
    return _mr_review_context(url=url, root=root, max_symbols=max_symbols)


@mcp.tool()
def mr_verify_review(url: str, window: int = 40) -> dict:
    """Check whether each review thread was addressed by nearby diff changes.

    Joins the review's discussions with its current diff: for every inline
    comment thread, surfaces the add/del lines within +/-window of the commented
    line. A thread with no nearby changes is a candidate for "not yet addressed";
    one with changes shows exactly what moved near it — a fast re-review pass
    without re-reading the whole diff. Read-only. Works on GitLab and GitHub
    (note: GitHub doesn't expose thread resolution via REST, so rely on
    has_changes there, not the resolved flag).

    Args:
        url:    Full merge/pull request URL.
        window: Lines before/after the commented line to scan (default 40).

    Returns:
        {mr_number, title, summary{total_threads, resolved, unresolved,
         threads_with_changes, threads_without_changes}, threads[{...,
         nearby_changes, has_changes}], threads_no_location[...]}
    """
    return _mr_verify_review(url=url, window=window)


@mcp.tool()
def mr_post_comments(url: str, comments: list[dict], max_comments: int = 25) -> dict:
    """Post inline review comments to a merge/pull request. WRITE — this mutates the review.

    The only mutating tool in myopic. Posts each comment one at a time from a
    queue, immediately visible (no drafts, no bulk-publish), retrying transient
    failures (HTTP 429/5xx) with exponential backoff — so partial progress
    survives a failure and self-hosted rate limits are respected. Works on
    GitLab and GitHub; the backend translates positions. Only call this on the
    user's explicit request to post — never speculatively.

    Get exact line numbers first from mr_diff_lines (its lines_filter maps a
    source line to the diff position). Each comment needs file_path, body, and at
    least one of new_line (added/unchanged line) or old_line (removed line).

    Args:
        url:      Full merge/pull request URL.
        comments: List of {file_path, body, new_line?, old_line?, old_path?}.
        max_comments: Safety cap per call (default 25); split larger batches.

    Returns:
        {url, platform, total, posted, failed, published, publish_error,
         details[{file_path, line, status, error}]} or {"error": "..."}.
    """
    return _mr_post_comments(url=url, comments=comments, max_comments=max_comments)


def main() -> None:
    """Entry point for `python -m myopic.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
