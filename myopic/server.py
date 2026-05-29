"""
myopic MCP server — FastMCP entry point.

Tools are registered here with full docstrings. The first line of every
docstring is self-contained — non-Claude clients show only that line.

Start with:
  python -m myopic.server
  uvx myopic
"""

from mcp.server.fastmcp import FastMCP

from myopic.tools.diff_lines import mr_diff_lines as _mr_diff_lines
from myopic.tools.review_status import mr_review_status as _mr_review_status

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
2. mr_diff_lines(url) — fetch the diff as structured, line-numbered hunks. Use
   this to read exactly what changed and to get the precise new-file line
   numbers needed to anchor inline comments. For large MRs, pass files_filter
   or lines_filter to keep the payload small.

Roadmap (not yet available): AST-grouped diffs for large reviews, RAG-augmented
review with callers/conventions of changed symbols, and bulk inline-comment
posting. See the project README/ROADMAP.

If a tool returns an "error" about configuration, the user has not set up a
GitLab URL + token yet. Tell them to run `myopic init` in their terminal — do
not attempt to configure it yourself.

Currently GitLab merge requests are supported; GitHub pull requests are planned.
""",
)


@mcp.tool()
def mr_diff_lines(
    url: str,
    files_filter: list[str] | None = None,
    lines_filter: dict[str, list[int]] | None = None,
) -> dict:
    """
    Fetch a merge request's diff as structured, line-numbered hunks.

    Pure data, no LLM. Returns exact file paths, old/new line numbers, and diff
    content — everything needed to read a change precisely and to compute the
    diff positions required for inline comments. For large reviews, narrow the
    payload with files_filter, or use lines_filter to get just the line mappings
    for the source lines you intend to comment on.

    Args:
        url:          Full GitLab merge request URL.
        files_filter: Optional list of file-path fragments to include.
        lines_filter: Optional map of filename-fragment -> target new-file line
                      numbers; returns compact line_mappings instead of full hunks.

    Returns:
        {mr_number, title, author, source_branch, target_branch, description,
         commits, diff_shas, files[...], stats}
    """
    return _mr_diff_lines(url=url, files_filter=files_filter, lines_filter=lines_filter)


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


def main() -> None:
    """Entry point for `python -m myopic.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
