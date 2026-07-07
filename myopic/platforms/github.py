"""
GitHub implementation of the review-platform interface.

Wraps PyGithub. Produces the exact same normalized ReviewMetadata / DiffSet /
DiscussionSet dataclasses as the GitLab backend, so every myopic tool works on a
GitHub pull request with zero changes. Public github.com and GitHub Enterprise
(via a configured base URL) are both supported.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from myopic.config import load_config
from myopic.platforms.base import (
    CommentDraft,
    DiffSet,
    Discussion,
    DiscussionSet,
    FileDiff,
    Note,
    Review,
    ReviewMetadata,
    ReviewPlatform,
)

logger = logging.getLogger(__name__)


def _ts(value) -> str:
    """Normalize a PyGithub datetime (or None) to an ISO string."""
    try:
        return value.isoformat() if value is not None else ""
    except AttributeError:
        return str(value or "")


class GitHubReview(Review):
    """A handle to one GitHub pull request."""

    platform_name = "github"

    def __init__(self, pr, pr_number: int, token: str = "") -> None:
        self._pr = pr
        self._number = pr_number
        self._token = token

    def _scrub_error(self, message: str) -> str:
        """Never let the token leak into a surfaced error message."""
        if self._token:
            message = message.replace(self._token, "***")
        return message

    def metadata(self) -> ReviewMetadata:
        commits: list[str] = []
        try:
            commits = [c.commit.message.splitlines()[0] for c in self._pr.get_commits()]
        except Exception:
            pass
        user = getattr(self._pr, "user", None)
        return ReviewMetadata(
            number=self._number,
            title=self._pr.title or "",
            author=getattr(user, "login", "unknown") if user else "unknown",
            source_branch=getattr(self._pr.head, "ref", ""),
            target_branch=getattr(self._pr.base, "ref", ""),
            description=self._pr.body or "",
            state=getattr(self._pr, "state", ""),
            merge_status=getattr(self._pr, "mergeable_state", "") or "",
            commits=commits,
        )

    def diffs(self) -> DiffSet:
        shas = {
            "base_sha": getattr(self._pr.base, "sha", ""),
            "head_sha": getattr(self._pr.head, "sha", ""),
            "start_sha": getattr(self._pr.base, "sha", ""),
        }

        files: list[FileDiff] = []
        try:
            for f in self._pr.get_files():
                status = getattr(f, "status", "")
                files.append(FileDiff(
                    file_path=f.filename,
                    old_path=getattr(f, "previous_filename", None) or f.filename,
                    new_file=status == "added",
                    deleted_file=status == "removed",
                    renamed_file=status == "renamed",
                    # GitHub omits `patch` for binary or very large files.
                    patch=getattr(f, "patch", None) or "",
                ))
        except Exception as exc:
            logger.error("Failed to fetch PR files: %s", exc)

        return DiffSet(files=files, shas=shas)

    def discussions(self) -> DiscussionSet:
        discussions: list[Discussion] = []
        general: list[Note] = []

        # Inline (resolvable) review comments — group reply chains into threads.
        try:
            threads: dict[int, dict] = {}   # root comment id -> thread dict
            order: list[int] = []
            for c in self._pr.get_review_comments():
                note = Note(
                    author=getattr(getattr(c, "user", None), "login", "unknown"),
                    body=c.body or "",
                    created_at=_ts(getattr(c, "created_at", None)),
                )
                reply_to = getattr(c, "in_reply_to_id", None)
                root = reply_to if reply_to in threads else c.id
                if root not in threads:
                    threads[root] = {
                        "id": c.id,
                        "file_path": getattr(c, "path", None),
                        "line": getattr(c, "line", None) or getattr(c, "original_line", None),
                        "notes": [],
                    }
                    order.append(root)
                threads[root]["notes"].append(note)

            for root in order:
                t = threads[root]
                discussions.append(Discussion(
                    id=str(t["id"]),
                    # GitHub thread-resolution status isn't exposed via REST; the
                    # GraphQL API is needed. Reported as unresolved for now.
                    resolved=False,
                    file_path=t["file_path"],
                    line=t["line"],
                    notes=t["notes"],
                ))
        except Exception as exc:
            logger.error("Failed to fetch review comments: %s", exc)

        # Free-floating PR conversation comments (non-inline).
        try:
            for ic in self._pr.get_issue_comments():
                general.append(Note(
                    author=getattr(getattr(ic, "user", None), "login", "unknown"),
                    body=ic.body or "",
                    created_at=_ts(getattr(ic, "created_at", None)),
                ))
        except Exception as exc:
            logger.error("Failed to fetch issue comments: %s", exc)

        return DiscussionSet(discussions=discussions, general_comments=general)

    def _post_one(self, comment: CommentDraft) -> None:
        """Post one inline review comment on the head commit, immediately.

        GitHub takes file-side line + side directly (RIGHT for an added/context
        line, LEFT for a removed one), anchored to the PR head SHA. Each call is
        an independent comment, so the driver can post from a queue and retry a
        single failure without touching the others. Raises on failure.
        """
        if comment.new_line is not None:
            line, side = comment.new_line, "RIGHT"
        else:
            line, side = comment.old_line, "LEFT"

        head_sha = getattr(self._pr.head, "sha", "")
        self._pr.create_review_comment(
            body=comment.body,
            commit=head_sha,   # PyGithub accepts a SHA string here
            path=comment.file_path,
            line=line,
            side=side,
        )


class GitHubPlatform(ReviewPlatform):
    """Opens GitHub pull requests from their web URLs."""

    name = "github"

    @classmethod
    def handles(cls, url: str) -> bool:
        return "/pull/" in url

    def _parse_url(self, url: str) -> tuple[str, int]:
        """Extract 'owner/repo' and the PR number from a PR web URL."""
        parts = urlparse(url).path.strip("/").split("/")
        # Expected: [owner, repo, "pull", number]
        if len(parts) < 4 or parts[2] != "pull":
            raise ValueError(f"Could not parse a GitHub PR from URL: {url}")
        try:
            number = int(parts[3])
        except ValueError as exc:
            raise ValueError(f"Could not parse a PR number from URL: {url}") from exc
        return f"{parts[0]}/{parts[1]}", number

    def open(self, url: str) -> GitHubReview:
        from github import Auth, Github

        cfg = load_config("github")
        full_name, number = self._parse_url(url)

        auth = Auth.Token(cfg.token)
        if cfg.url:  # GitHub Enterprise base host
            base = cfg.url if cfg.url.endswith("/api/v3") else cfg.url.rstrip("/") + "/api/v3"
            client = Github(base_url=base, auth=auth)
        else:
            client = Github(auth=auth)

        try:
            repo = client.get_repo(full_name)
            pr = repo.get_pull(number)
        except Exception as exc:
            # Scrub the token out of any error text before surfacing it.
            msg = str(exc).replace(cfg.token, "***") if cfg.token else str(exc)
            raise RuntimeError(
                f"GitHub request failed for {full_name}#{number}: {msg.splitlines()[0][:160]}. "
                "Check your token (GITHUB_TOKEN / config [github].token) and repo access."
            ) from exc

        return GitHubReview(pr, number, token=cfg.token)
