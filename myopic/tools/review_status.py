"""
mr_review_status — metadata + discussions + resolution state in one call.

Pure data extraction, no LLM. Collapses what would be 5-6 separate platform API
calls (get MR, list discussions, list changes, list commits...) into a single
structured snapshot of where a review stands: what's been raised, what's
resolved, and what still needs attention.
"""

from __future__ import annotations

from dataclasses import asdict

from myopic.diff import count_lines
from myopic.platforms.base import open_review


def mr_review_status(url: str) -> dict:
    """
    Fetch a review's status: metadata + discussions + resolution state.

    Args:
        url: Full review URL (GitLab merge request).

    Returns: mr_number, title, author, branches, description, state,
        merge_status, commits, general_comments, discussions[{id, resolved,
        file_path, line, notes}], summary{total_discussions, resolved,
        unresolved, unresolved_details}, files_changed[{file_path, additions,
        deletions}], stats.
    """
    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    try:
        meta = review.metadata()
        disc_set = review.discussions()
        diff_set = review.diffs()
    except Exception as exc:
        return {"error": f"Failed to fetch review status: {exc}"}

    resolved_count = 0
    unresolved_count = 0
    unresolved_details: list[dict] = []
    discussions: list[dict] = []

    for disc in disc_set.discussions:
        discussions.append({
            "id": disc.id,
            "resolved": disc.resolved,
            "file_path": disc.file_path,
            "line": disc.line,
            "notes": [asdict(n) for n in disc.notes],
        })
        if disc.resolved:
            resolved_count += 1
        else:
            unresolved_count += 1
            preview = disc.notes[0].body[:150] if disc.notes else ""
            unresolved_details.append({
                "id": disc.id,
                "file_path": disc.file_path,
                "first_comment_preview": preview,
            })

    files_changed: list[dict] = []
    total_adds = 0
    total_dels = 0
    for fd in diff_set.files:
        adds, dels = count_lines(fd.patch)
        total_adds += adds
        total_dels += dels
        files_changed.append({
            "file_path": fd.file_path,
            "additions": adds,
            "deletions": dels,
        })

    return {
        "mr_number": meta.number,
        "title": meta.title,
        "author": meta.author,
        "source_branch": meta.source_branch,
        "target_branch": meta.target_branch,
        "description": meta.description,
        "state": meta.state,
        "merge_status": meta.merge_status,
        "commits": meta.commits,
        "general_comments": [asdict(n) for n in disc_set.general_comments],
        "discussions": discussions,
        "summary": {
            "total_discussions": len(discussions),
            "resolved": resolved_count,
            "unresolved": unresolved_count,
            "unresolved_details": unresolved_details,
        },
        "files_changed": files_changed,
        "stats": {
            "total_files": len(files_changed),
            "total_additions": total_adds,
            "total_deletions": total_dels,
        },
    }
