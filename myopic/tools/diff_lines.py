"""
mr_diff_lines — fetch a review's diff as structured, line-numbered data.

Pure data extraction, no LLM. Returns exact file paths, line numbers, and diff
content that a calling agent can use to post inline review comments at the
correct position. For large reviews, `lines_filter` returns only the line
mappings you need instead of the entire diff, keeping the payload small.
"""

from __future__ import annotations

import json

from myopic.diff import classify_file, count_lines, find_line_mappings, parse_hunks
from myopic.platforms.base import open_review


def mr_diff_lines(
    url: str,
    files_filter: list[str] | None = None,
    lines_filter: dict[str, list[int]] | None = None,
    max_chars: int = 80000,
    skip_noise: bool = True,
) -> dict:
    """
    Fetch a merge/pull request diff and return structured line-level data.

    Args:
        url: Full review URL (GitLab merge request).
        files_filter: Optional list of file-path fragments to include. Only
            files matching any entry (substring or suffix) are returned. Use on
            large reviews to stay under token limits.
        lines_filter: Optional map of filename-fragment -> list of target
            new-file line numbers. When given, returns compact `line_mappings`
            (the closest diff-side line for each requested number) instead of
            full hunks — ideal when you only need exact positions to post a few
            inline comments.

    Returns (full mode): mr_number, title, author, branches, description,
        commits, diff_shas, files[{file_path, old_path, new_file, deleted_file,
        renamed_file, additions, deletions, hunks}], stats.
    Returns (lines_filter mode): same header, files[{file_path, line_mappings}].
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

    combined_filter: list[str] | None = None
    if files_filter or lines_filter:
        combined_filter = list(files_filter or []) + list(
            lines_filter.keys() if lines_filter else []
        )

    # An explicit filter is an intentional targeted fetch — honor it fully: no
    # noise-skipping and no budget truncation, or the caller can't get what they
    # asked for. Noise/budget guards only apply to an untargeted full fetch.
    targeted = bool(combined_filter)

    files: list[dict] = []
    omitted_files: list[dict] = []   # reviewable but cut for the budget — refetch via files_filter
    skipped_files: list[dict] = []   # noise — kept out of the body
    total_adds = 0
    total_dels = 0
    used_chars = 0
    budget_hit = False

    for fd in diff_set.files:
        file_path = fd.file_path

        if combined_filter and not any(
            f in file_path or file_path.endswith(f) for f in combined_filter
        ):
            continue

        additions, deletions = count_lines(fd.patch)
        total_adds += additions
        total_dels += deletions

        if not targeted:
            reviewable, skip_reason = classify_file(file_path, additions, deletions)
            if skip_noise and not reviewable:
                skipped_files.append({
                    "file_path": file_path, "additions": additions,
                    "deletions": deletions, "reason": skip_reason,
                })
                continue
            if budget_hit:
                omitted_files.append({
                    "file_path": file_path, "additions": additions,
                    "deletions": deletions, "reason": "token budget reached",
                })
                continue

        hunks = parse_hunks(fd.patch)

        if lines_filter:
            target_lines: list[int] = []
            for key, line_nums in lines_filter.items():
                if key in file_path or file_path.endswith(key):
                    target_lines.extend(line_nums)
            files.append({
                "file_path": file_path,
                "line_mappings": find_line_mappings(hunks, target_lines),
            })
            continue

        file_entry = {
            "file_path": file_path,
            "old_path": fd.old_path,
            "new_file": fd.new_file,
            "deleted_file": fd.deleted_file,
            "renamed_file": fd.renamed_file,
            "additions": additions,
            "deletions": deletions,
            "hunks": hunks,
        }

        # Budget check (untargeted only). Always emit at least one file so a single
        # over-budget file still returns something rather than an empty result.
        if not targeted:
            entry_chars = len(json.dumps(file_entry))
            if files and used_chars + entry_chars > max_chars:
                budget_hit = True
                omitted_files.append({
                    "file_path": file_path, "additions": additions,
                    "deletions": deletions, "reason": "token budget reached",
                })
                continue
            used_chars += entry_chars

        files.append(file_entry)

    result = {
        "mr_number": meta.number,
        "title": meta.title,
        "author": meta.author,
        "source_branch": meta.source_branch,
        "target_branch": meta.target_branch,
        "description": meta.description,
        "commits": meta.commits,
        "diff_shas": diff_set.shas,
        "files": files,
        "truncated": budget_hit,
        "omitted_files": omitted_files,
        "skipped_files": skipped_files,
        "stats": {
            "files_returned": len(files),
            "files_omitted": len(omitted_files),
            "files_skipped": len(skipped_files),
            "total_additions": total_adds,
            "total_deletions": total_dels,
        },
    }
    if budget_hit:
        result["next"] = (
            f"Output hit the {max_chars}-char budget; {len(omitted_files)} file(s) "
            "not returned. Fetch them with "
            "mr_diff_lines(url, files_filter=[<file_path from omitted_files>, ...])."
        )
    return result
