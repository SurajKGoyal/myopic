"""
Shared "next step" guidance so the tools self-guide the review workflow.

myopic can't rely on a caller's prompt knowing the flow — a stranger's agent only
knows what the tool results tell it. So the read-the-diff tools point onward to
reviewing against the whole codebase; otherwise an agent stops at the diff and
never reaches myopic's actual value.
"""

REVIEW_AGAINST_CODEBASE = (
    "Now review this against the WHOLE codebase, not just the diff: "
    "mr_review_context(url, root) on a local checkout of the MR returns each changed "
    "symbol's blast radius — who else calls it (dependency_impact, no index needed) — "
    "and, with an index, similar existing code (duplication / "
    "convention drift). Point `root` at a checkout of the MR branch (see myopic worktree)."
)
