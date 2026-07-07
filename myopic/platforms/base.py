"""
Platform abstraction seam.

myopic is GitLab-first today, but merge requests and pull requests are the same
idea wearing different clothes. Every tool talks to this normalized interface,
never to a platform SDK directly, so adding GitHub later is a new `Review`
implementation — not a rewrite of the tools.

A `Review` is a handle to one merge/pull request. The factory `open_review(url)`
inspects the URL, picks the registered platform that handles it, and returns a
ready-to-query handle.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field

# HTTP statuses worth retrying: rate-limit (429) and transient server errors.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status from a platform SDK exception.

    python-gitlab exposes it as `response_code`, PyGithub as `status`.
    """
    for attr in ("response_code", "status"):
        code = getattr(exc, attr, None)
        if isinstance(code, int):
            return code
    return None


def _is_transient(exc: Exception) -> bool:
    """True if an error is worth retrying (rate limit, 5xx, or a network blip)."""
    code = _status_of(exc)
    if code is not None:
        return code in _RETRY_STATUS
    name = type(exc).__name__.lower()
    return "timeout" in name or "connection" in name


@dataclass
class ReviewMetadata:
    """Platform-neutral metadata for a merge/pull request."""

    number: int
    title: str
    author: str
    source_branch: str
    target_branch: str
    description: str = ""
    state: str = ""
    merge_status: str = ""
    commits: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    """One changed file within a review, with its raw unified-diff patch."""

    file_path: str
    old_path: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    patch: str = ""


@dataclass
class DiffSet:
    """All file diffs for a review, plus the SHAs needed to anchor comments."""

    files: list[FileDiff] = field(default_factory=list)
    # base/head/start SHAs — required by some platforms to post inline comments.
    shas: dict | None = None


@dataclass
class Note:
    """A single comment within a discussion thread."""

    author: str
    body: str
    created_at: str = ""
    system: bool = False


@dataclass
class Discussion:
    """A resolvable review thread anchored to a file/line."""

    id: str
    resolved: bool
    file_path: str | None
    line: int | None
    notes: list[Note] = field(default_factory=list)


@dataclass
class DiscussionSet:
    """Resolvable discussions plus free-floating general comments."""

    discussions: list[Discussion] = field(default_factory=list)
    general_comments: list[Note] = field(default_factory=list)


@dataclass
class CommentDraft:
    """A normalized inline review comment to post at a file line.

    Line numbers are file-side (as reported by mr_diff_lines), not diff
    positions — each backend translates them to what its API needs. Provide
    `new_line` for a comment on an added/context line, `old_line` for a removed
    line. `old_path` matters only for renamed files (GitLab positioning).
    """

    file_path: str
    body: str
    new_line: int | None = None
    old_line: int | None = None
    old_path: str | None = None


@dataclass
class PostOutcome:
    """The result of attempting to post one comment."""

    file_path: str
    line: int | None
    status: str  # "posted" | "failed"
    error: str | None = None


@dataclass
class PostResult:
    """The outcome of a bulk inline-comment post."""

    total: int
    posted: int
    failed: int
    published: bool
    publish_error: str | None = None
    details: list[PostOutcome] = field(default_factory=list)


class Review(ABC):
    """A handle to a single merge/pull request on some platform."""

    # Set by each concrete backend; surfaced in tool output.
    platform_name: str = ""

    @abstractmethod
    def metadata(self) -> ReviewMetadata:
        """Return platform-neutral metadata for this review."""

    @abstractmethod
    def diffs(self) -> DiffSet:
        """Return all file diffs (with patches) and the anchoring SHAs."""

    @abstractmethod
    def discussions(self) -> DiscussionSet:
        """Return resolvable discussion threads and general comments."""

    @abstractmethod
    def _post_one(self, comment: CommentDraft) -> None:
        """Post a single inline comment immediately. Raise on failure.

        Backends implement only this; the queue, pacing, and retry policy live
        in `post_comments` so behavior is identical across platforms.
        """

    def _scrub_error(self, message: str) -> str:
        """Hook to redact secrets from an error message. Identity by default."""
        return message

    def post_comments(
        self,
        comments: list[CommentDraft],
        *,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        max_retries: int = 4,
        gap: float = 0.25,
    ) -> PostResult:
        """Post inline review comments — the only mutating operation.

        Comments are drained from a FIFO queue and posted one at a time via
        `_post_one`. There is deliberately no batching and no bulk-publish step:
        each comment is an independent, immediately-visible post, so partial
        progress survives a mid-run failure. Transient failures (HTTP 429/5xx or
        a network blip) are retried with exponential backoff (base_delay,
        doubling up to max_delay, at most max_retries times); permanent failures
        (e.g. a 422 bad position) fail fast. A small `gap` paces successful posts
        to stay under rate limits.
        """
        queue: deque[CommentDraft] = deque(comments)
        details: list[PostOutcome] = []
        posted = failed = 0

        while queue:
            comment = queue.popleft()
            line = comment.new_line or comment.old_line
            delay = base_delay
            last_exc: Exception | None = None

            for attempt in range(max_retries + 1):
                try:
                    self._post_one(comment)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 — recorded per comment
                    last_exc = exc
                    if attempt == max_retries or not _is_transient(exc):
                        break
                    time.sleep(min(delay, max_delay))
                    delay *= 2

            if last_exc is None:
                details.append(PostOutcome(comment.file_path, line, "posted"))
                posted += 1
                if queue and gap:
                    time.sleep(gap)  # gentle pacing between successful posts
            else:
                details.append(PostOutcome(
                    comment.file_path, line, "failed",
                    self._scrub_error(str(last_exc))[:200],
                ))
                failed += 1

        return PostResult(
            total=len(comments),
            posted=posted,
            failed=failed,
            published=posted > 0,
            publish_error=None,
            details=details,
        )


class ReviewPlatform(ABC):
    """A code-review platform capable of opening reviews from their URLs."""

    name: str

    @classmethod
    @abstractmethod
    def handles(cls, url: str) -> bool:
        """Return True if this platform recognizes the given review URL."""

    @abstractmethod
    def open(self, url: str) -> Review:
        """Open a review handle for the given URL."""


def open_review(url: str) -> Review:
    """
    Open a review handle for any supported platform URL.

    Imports are local to keep optional platform SDKs from loading until needed.
    """
    from myopic.platforms.github import GitHubPlatform
    from myopic.platforms.gitlab import GitLabPlatform

    platforms: list[type[ReviewPlatform]] = [GitLabPlatform, GitHubPlatform]

    for platform_cls in platforms:
        if platform_cls.handles(url):
            return platform_cls().open(url)

    supported = ", ".join(p.name for p in platforms)
    raise ValueError(
        f"No registered platform recognizes the URL: {url}\n"
        f"Supported platforms: {supported}."
    )
