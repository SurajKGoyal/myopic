"""
GitLab implementation of the review-platform interface.

Wraps python-gitlab. Fetches the merge request once on `open()` and lazily
derives metadata, diffs, and discussions from it. The diff-version object is
cached so the diff and SHAs are fetched a single time per review.
"""

from __future__ import annotations

import logging
import re

from myopic.config import load_config
from myopic.diff import count_lines
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

_MR_NUMBER_RE = re.compile(r"merge_requests/(\d+)")


class GitLabReview(Review):
    """A handle to one GitLab merge request."""

    platform_name = "gitlab"

    def __init__(self, mr, mr_number: int) -> None:
        self._mr = mr
        self._number = mr_number
        self._diff_version = None
        self._diff_version_loaded = False
        self._shas = None
        self._shas_loaded = False

    def _latest_diff_version(self):
        """Fetch (once) the latest diff version object for this MR."""
        if self._diff_version_loaded:
            return self._diff_version
        self._diff_version_loaded = True
        try:
            versions = self._mr.diffs.list(get_all=True)
            if versions:
                # GitLab may not guarantee order; pick the highest id = newest.
                latest = max(versions, key=lambda v: v.id)
                self._diff_version = self._mr.diffs.get(latest.id)
        except Exception as exc:
            logger.warning("Could not fetch diff versions: %s", exc)
        return self._diff_version

    def metadata(self) -> ReviewMetadata:
        commits: list[str] = []
        try:
            commits = [c.title for c in self._mr.commits()]
        except Exception:
            pass
        return ReviewMetadata(
            number=self._number,
            title=self._mr.title,
            author=(self._mr.author or {}).get("username", "unknown"),
            source_branch=self._mr.source_branch,
            target_branch=self._mr.target_branch,
            description=self._mr.description or "",
            state=getattr(self._mr, "state", ""),
            merge_status=getattr(self._mr, "detailed_merge_status", ""),
            commits=commits,
        )

    def diffs(self) -> DiffSet:
        version = self._latest_diff_version()

        shas = None
        raw_changes: list[dict] = []
        if version is not None:
            try:
                attrs = version._attrs
                shas = {
                    "base_sha": attrs.get("base_commit_sha", ""),
                    "head_sha": attrs.get("head_commit_sha", ""),
                    "start_sha": attrs.get("start_commit_sha", ""),
                }
                raw_changes = attrs.get("diffs", []) or []
            except Exception as exc:
                logger.warning("Could not read diff version attrs: %s", exc)

        # Fallback to the changes() API if the diff version had nothing usable.
        if not raw_changes:
            try:
                raw = self._mr.changes()
                raw_changes = raw.get("changes", []) if isinstance(raw, dict) else []
            except Exception as exc:
                logger.error("Failed to fetch MR changes: %s", exc)

        files = [
            FileDiff(
                file_path=c.get("new_path") or c.get("old_path", "unknown"),
                old_path=c.get("old_path", c.get("new_path", "unknown")),
                new_file=bool(c.get("new_file", False)),
                deleted_file=bool(c.get("deleted_file", False)),
                renamed_file=bool(c.get("renamed_file", False)),
                patch=c.get("diff", ""),
            )
            for c in raw_changes
        ]
        return DiffSet(files=files, shas=shas)

    def discussions(self) -> DiscussionSet:
        discussions: list[Discussion] = []
        general: list[Note] = []

        try:
            raw_discussions = self._mr.discussions.list(get_all=True)
        except Exception as exc:
            logger.error("Failed to fetch discussions: %s", exc)
            return DiscussionSet()

        for disc in raw_discussions:
            notes = disc.attributes.get("notes", [])
            non_system = [n for n in notes if not n.get("system", False)]
            if not non_system:
                continue

            first = non_system[0]
            if not first.get("resolvable", False):
                # Free-floating MR comments — not anchored, not resolvable.
                general.extend(
                    Note(
                        author=n.get("author", {}).get("username", "unknown"),
                        body=n.get("body", ""),
                        created_at=n.get("created_at", ""),
                    )
                    for n in non_system
                )
                continue

            file_path = None
            line = None
            position = first.get("position")
            if position:
                file_path = position.get("new_path") or position.get("old_path")
                line = position.get("new_line") or position.get("old_line")

            parsed: list[Note] = []
            for n in notes:
                if n.get("system", False) and "changed this line" not in n.get("body", ""):
                    continue
                parsed.append(Note(
                    author=n.get("author", {}).get("username", "unknown"),
                    body=n.get("body", ""),
                    created_at=n.get("created_at", ""),
                    system=n.get("system", False),
                ))

            discussions.append(Discussion(
                id=disc.id,
                resolved=first.get("resolved", False),
                file_path=file_path,
                line=line,
                notes=parsed,
            ))

        return DiscussionSet(discussions=discussions, general_comments=general)

    def _post_shas(self) -> dict:
        """Fetch (once) the base/head/start SHAs an inline comment must anchor to."""
        if not self._shas_loaded:
            self._shas_loaded = True
            self._shas = self.diffs().shas or {}
        return self._shas

    def _post_one(self, comment: CommentDraft) -> None:
        """Post one inline comment as a resolvable MR discussion, immediately.

        No draft note and no bulk-publish — a positioned `discussions.create`
        is visible at once, which is what lets the queue post one at a time and
        preserve partial progress. Raises on failure so the driver can retry.
        """
        shas = self._post_shas()
        if not shas.get("base_sha"):
            raise RuntimeError(
                "Could not resolve diff SHAs — needed to position an inline comment."
            )

        position = {
            "position_type": "text",
            "base_sha": shas.get("base_sha", ""),
            "head_sha": shas.get("head_sha", ""),
            "start_sha": shas.get("start_sha", ""),
            "new_path": comment.file_path,
            "old_path": comment.old_path or comment.file_path,
        }
        if comment.new_line:
            position["new_line"] = comment.new_line
        if comment.old_line:
            position["old_line"] = comment.old_line

        self._mr.discussions.create({"body": comment.body, "position": position})


class GitLabPlatform(ReviewPlatform):
    """Opens GitLab merge requests from their web URLs."""

    name = "gitlab"

    @classmethod
    def handles(cls, url: str) -> bool:
        return "merge_requests/" in url

    def _parse_url(self, url: str) -> tuple[str, int]:
        match = _MR_NUMBER_RE.search(url)
        if not match:
            raise ValueError(f"Could not parse MR number from URL: {url}")
        mr_number = int(match.group(1))
        cfg = load_config()
        path = url.replace(cfg.url, "").strip("/")
        repo = path.split("/-/")[0]
        return repo, mr_number

    def open(self, url: str) -> GitLabReview:
        import gitlab

        cfg = load_config()
        repo, mr_number = self._parse_url(url)

        client = gitlab.Gitlab(url=cfg.url, private_token=cfg.token)
        try:
            client.auth()
        except gitlab.exceptions.GitlabAuthenticationError as exc:
            # Never surface the raw error — some versions echo the token.
            raise RuntimeError(
                f"GitLab authentication failed for {cfg.url}. "
                "Check your token in the myopic config or GITLAB_TOKEN."
            ) from exc

        project = client.projects.get(repo)
        mr = project.mergerequests.get(mr_number)
        return GitLabReview(mr, mr_number)
