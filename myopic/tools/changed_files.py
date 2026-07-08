"""
mr_changed_files — a content-free manifest of a review's changed files.

The cheap entry point for any large review: it returns file paths, stats, and a
reviewability classification but NONE of the actual diff lines, so the payload
stays small no matter how big the review is. Use it first to see the shape, then
fetch real diffs a batch at a time with mr_diff_lines(files_filter=[...]).
"""

from __future__ import annotations

from myopic.diff import classify_file, count_lines
from myopic.platforms.base import open_review


def mr_changed_files(url: str) -> dict:
    """List the files changed in a review with stats — no diff content.

    Each file reports additions/deletions, new/deleted/renamed flags, and a
    reviewability classification (lockfiles, generated code, binary assets, and
    enormous single-file changes are flagged reviewable=false with a skip_reason).
    Files are ordered reviewable-first, then largest-change-first, so a caller can
    batch the highest-value files straight into mr_diff_lines(files_filter=[...]).
    """
    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    try:
        meta = review.metadata()
        diff_set = review.diffs()
    except Exception as exc:
        return {"error": f"Failed to fetch diff: {exc}"}

    files: list[dict] = []
    total_adds = total_dels = 0
    review_adds = review_dels = 0

    for fd in diff_set.files:
        additions, deletions = count_lines(fd.patch)
        total_adds += additions
        total_dels += deletions

        reviewable, skip_reason = classify_file(fd.file_path, additions, deletions)
        if reviewable:
            review_adds += additions
            review_dels += deletions

        files.append({
            "file_path": fd.file_path,
            "old_path": fd.old_path,
            "new_file": fd.new_file,
            "deleted_file": fd.deleted_file,
            "renamed_file": fd.renamed_file,
            "additions": additions,
            "deletions": deletions,
            "reviewable": reviewable,
            "skip_reason": skip_reason,
        })

    # Reviewable first, then biggest change first.
    files.sort(key=lambda f: (not f["reviewable"], -(f["additions"] + f["deletions"])))

    return {
        "mr_number": meta.number,
        "title": meta.title,
        "author": meta.author,
        "source_branch": meta.source_branch,
        "target_branch": meta.target_branch,
        "description": meta.description,
        "commits": meta.commits,
        "diff_shas": diff_set.shas,
        "files": files,
        "stats": {
            "total_files": len(files),
            "reviewable_files": sum(1 for f in files if f["reviewable"]),
            "skipped_files": sum(1 for f in files if not f["reviewable"]),
            "total_additions": total_adds,
            "total_deletions": total_dels,
            "reviewable_additions": review_adds,
            "reviewable_deletions": review_dels,
        },
        "next": (
            "Read the change: mr_diff_sections(url) (large MRs) or "
            "mr_diff_lines(url, files_filter=[<file_path>, ...]) to batch the reviewable "
            "files. THEN review it against the whole codebase — mr_review_context(url, "
            "root) on a local checkout gives each changed symbol's blast radius (no "
            "index needed). Reading the diff alone is only half a review."
        ),
    }
