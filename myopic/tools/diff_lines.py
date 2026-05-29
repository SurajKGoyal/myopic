"""
mr_diff_lines — fetch a review's diff as structured, line-numbered data.

Pure data extraction, no LLM. Returns exact file paths, line numbers, and diff
content that a calling agent can use to post inline review comments at the
correct position. For large reviews, `lines_filter` returns only the line
mappings you need instead of the entire diff, keeping the payload small.
"""

from __future__ import annotations

from myopic.diff import count_lines, find_line_mappings, parse_hunks
from myopic.platforms.base import open_review


def mr_diff_lines(
    url: str,
    files_filter: list[str] | None = None,
    lines_filter: dict[str, list[int]] | None = None,
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

    files: list[dict] = []
    total_adds = 0
    total_dels = 0

    for fd in diff_set.files:
        file_path = fd.file_path

        if combined_filter and not any(
            f in file_path or file_path.endswith(f) for f in combined_filter
        ):
            continue

        additions, deletions = count_lines(fd.patch)
        total_adds += additions
        total_dels += deletions
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
        else:
            files.append({
                "file_path": file_path,
                "old_path": fd.old_path,
                "new_file": fd.new_file,
                "deleted_file": fd.deleted_file,
                "renamed_file": fd.renamed_file,
                "additions": additions,
                "deletions": deletions,
                "hunks": hunks,
            })

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
            "total_additions": total_adds,
            "total_deletions": total_dels,
        },
    }
