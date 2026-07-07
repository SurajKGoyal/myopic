"""
mr_post_comments — post inline review comments to a merge/pull request.

The only mutating tool in myopic. It validates the requested comments, opens the
review, and hands them to the platform's queue-and-backoff poster: each comment
is posted one at a time and immediately visible (no drafts, no bulk-publish), so
partial progress survives a failure and rate limits are respected. Works
identically on GitLab and GitHub — the platform backend translates positions.

No LLM — pure validation + API calls.
"""

from __future__ import annotations

from myopic.platforms.base import CommentDraft, open_review

MAX_COMMENTS = 25


def mr_post_comments(url: str, comments: list[dict], max_comments: int = MAX_COMMENTS) -> dict:
    """
    Post inline review comments to a merge/pull request.

    Args:
        url: Full review URL (GitLab merge request or GitHub pull request).
        comments: List of comment objects, each with:
            - file_path (str, required): path to the file in the review.
            - body (str, required): the comment text (markdown allowed).
            - new_line (int, optional): line number in the new file version
              (a comment on an added or unchanged line).
            - old_line (int, optional): line number in the old file version
              (a comment on a removed line).
            - old_path (str, optional): the pre-rename path, for renamed files.
          At least one of new_line / old_line is required. Get exact line
          numbers from mr_diff_lines (its lines_filter resolves a source line to
          the correct diff position).
        max_comments: Safety cap on comments per call (default 25). Split larger
          reviews into multiple calls.

    Returns:
        {url, platform, total, posted, failed, published, publish_error,
         details[{file_path, line, status: "posted"|"failed", error}]}
        or {"error": "..."} on a validation or open failure.
    """
    if not comments:
        return {"error": "No comments provided."}
    if len(comments) > max_comments:
        return {
            "error": f"Too many comments ({len(comments)}). Max is {max_comments} "
            "per call — split them into batches."
        }

    drafts: list[CommentDraft] = []
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            return {"error": f"Comment {i} must be an object."}
        if not c.get("file_path"):
            return {"error": f"Comment {i} is missing required field: file_path."}
        if not c.get("body"):
            return {"error": f"Comment {i} is missing required field: body."}
        if not c.get("new_line") and not c.get("old_line"):
            return {
                "error": f"Comment {i} needs at least one of new_line / old_line. "
                "Get exact line numbers from mr_diff_lines."
            }
        drafts.append(CommentDraft(
            file_path=c["file_path"],
            body=c["body"],
            new_line=c.get("new_line"),
            old_line=c.get("old_line"),
            old_path=c.get("old_path"),
        ))

    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    try:
        result = review.post_comments(drafts)
    except Exception as exc:
        return {"error": f"Failed to post comments: {exc}"}

    return {
        "url": url,
        "platform": getattr(review, "platform_name", ""),
        "total": result.total,
        "posted": result.posted,
        "failed": result.failed,
        "published": result.published,
        "publish_error": result.publish_error,
        "details": [
            {"file_path": d.file_path, "line": d.line, "status": d.status, "error": d.error}
            for d in result.details
        ],
    }
