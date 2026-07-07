"""
mr_verify_review — did later commits actually address each review thread?

Joins the normalized discussions and diff of a review: for every inline comment
thread it surfaces the changed lines near the originally-commented line, so you
can see at a glance whether an issue was touched without re-reading the whole
diff. Platform-neutral by construction — it uses only the normalized Review
interface (discussions + diffs), so it works on GitLab and GitHub alike.

No LLM — pure data joining.
"""

from __future__ import annotations

from myopic.diff import parse_hunks
from myopic.platforms.base import open_review


def mr_verify_review(url: str, window: int = 40) -> dict:
    """
    Pair each review discussion with nearby diff changes to check if it was addressed.

    For each inline comment thread, returns the add/del lines within +/-window of
    the originally-commented line. A thread with no nearby changes is a candidate
    for "not yet addressed"; one with changes shows exactly what moved near it.

    Note: GitHub does not expose thread-resolution status over its REST API, so
    for GitHub PRs `resolved` is reported as false — rely on has_changes /
    nearby_changes there, not the resolved flag.

    Args:
        url: Full review URL (GitLab merge request or GitHub pull request).
        window: Lines before/after the commented line to scan for changes
                (default 40). Increase for large refactors where lines shift.

    Returns:
        {mr_number, title,
         summary{total_threads, resolved, unresolved, threads_with_changes,
                 threads_without_changes},
         threads[{id, resolved, file_path, original_line, first_comment,
                  replies, nearby_changes[{type, old_line, new_line, content,
                  distance}], has_changes}],
         threads_no_location[{id, resolved, first_comment, replies}]}
        or {"error": "..."} on failure.
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
        return {"error": f"Failed to fetch review data: {exc}"}

    # 1. Normalize discussions into located (inline) and unlocated threads,
    #    de-duplicating identical comments on the same file+line.
    threads: list[dict] = []
    threads_no_loc: list[dict] = []
    seen: set[tuple] = set()

    for disc in disc_set.discussions:
        non_system = [n for n in disc.notes if not n.system]
        if not non_system:
            continue
        first = non_system[0]

        dedup_key = (disc.file_path, disc.line, first.body[:200])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        entry = {
            "id": disc.id,
            "resolved": disc.resolved,
            "file_path": disc.file_path,
            "original_line": disc.line,
            "first_comment": {"author": first.author, "body": first.body},
            "replies": [{"author": n.author, "body": n.body} for n in non_system[1:]],
        }
        if disc.file_path and disc.line is not None:
            threads.append(entry)
        else:
            threads_no_loc.append(entry)

    # 2. Parse changed lines only for files that actually have inline threads.
    comment_files = list({t["file_path"] for t in threads if t["file_path"]})
    file_changed_lines: dict[str, list[dict]] = {}
    if comment_files:
        for fd in diff_set.files:
            fp = fd.file_path
            if not any(cf in fp or fp.endswith(cf) for cf in comment_files):
                continue
            if not fd.patch:
                continue
            changed = [
                ln
                for hunk in parse_hunks(fd.patch)
                for ln in hunk["lines"]
                if ln["type"] in ("add", "del")
            ]
            file_changed_lines[fp] = changed

    def _changed_for(file_path: str) -> list[dict]:
        if file_path in file_changed_lines:
            return file_changed_lines[file_path]
        for fp, lines in file_changed_lines.items():
            if fp.endswith(file_path) or file_path.endswith(fp):
                return lines
        return []

    # 3. For each located thread, collect changes within the window.
    with_changes = 0
    without_changes = 0
    for t in threads:
        orig = t["original_line"]
        nearby: list[dict] = []
        for ln in _changed_for(t["file_path"]):
            ref = ln["new_line"] if ln["new_line"] is not None else ln["old_line"]
            if ref is None:
                continue
            dist = abs(ref - orig)
            if dist <= window:
                nearby.append({
                    "type": ln["type"],
                    "old_line": ln["old_line"],
                    "new_line": ln["new_line"],
                    "content": ln["content"],
                    "distance": dist,
                })
        nearby.sort(key=lambda x: x["distance"])
        t["nearby_changes"] = nearby
        t["has_changes"] = bool(nearby)
        if nearby:
            with_changes += 1
        else:
            without_changes += 1

    resolved = sum(1 for t in threads if t["resolved"])
    return {
        "mr_number": meta.number,
        "title": meta.title,
        "summary": {
            "total_threads": len(threads),
            "resolved": resolved,
            "unresolved": len(threads) - resolved,
            "threads_with_changes": with_changes,
            "threads_without_changes": without_changes,
        },
        "threads": threads,
        "threads_no_location": threads_no_loc,
    }
