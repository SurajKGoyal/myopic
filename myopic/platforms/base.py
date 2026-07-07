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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class Review(ABC):
    """A handle to a single merge/pull request on some platform."""

    @abstractmethod
    def metadata(self) -> ReviewMetadata:
        """Return platform-neutral metadata for this review."""

    @abstractmethod
    def diffs(self) -> DiffSet:
        """Return all file diffs (with patches) and the anchoring SHAs."""

    @abstractmethod
    def discussions(self) -> DiscussionSet:
        """Return resolvable discussion threads and general comments."""


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
