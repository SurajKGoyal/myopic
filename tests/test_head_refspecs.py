"""
Fetching a review's head when the source branch lives on a fork.

A fork's branch does not exist on the target remote, so `git fetch origin
<branch>` cannot reach the head and `myopic worktree` failed on every external
contribution. GitHub publishes the head under `refs/pull/N/head` in the *base*
repository, which resolves branch and fork PRs alike; these tests pin that ref
being tried first and the CLI falling through the candidates until the head is
local.

GitLab deliberately keeps the inherited branch-only behavior: the fork case was
never reproduced there and its head-ref equivalent is unverified against a live
instance, so the working path is left untouched. `test_gitlab_keeps_branch_only`
pins that as a decision rather than an oversight.
"""

from __future__ import annotations

from click.testing import CliRunner

from myopic import cli as cli_mod
from myopic.platforms.base import Review, ReviewMetadata
from myopic.platforms.github import GitHubReview
from myopic.platforms.gitlab import GitLabReview


class _Ref:
    def __init__(self, ref): self.ref = ref


class _PR:
    def __init__(self, branch="feat/x"): self.head = _Ref(branch)


class _MR:
    """Enough of a python-gitlab MR for metadata() — the base default reads it."""

    title = "t"
    target_branch = "main"
    description = ""
    state = "opened"
    detailed_merge_status = "mergeable"

    def __init__(self, branch="feat/x"):
        self.source_branch = branch
        self.author = {"username": "a"}

    def commits(self):
        return []


# --- per-platform ordering ---------------------------------------------------

class TestRefspecOrdering:
    def test_github_prefers_pull_head_then_branch(self):
        refs = GitHubReview(_PR(), 17).head_refspecs()
        assert refs == ["pull/17/head", "feat/x"]

    def test_gitlab_keeps_branch_only(self):
        # Deliberate: GitLab is untouched by this change. If someone adds an
        # MR-head ref later, this test should be updated, not deleted silently.
        assert GitLabReview.head_refspecs is Review.head_refspecs
        assert GitLabReview(_MR(), 42).head_refspecs() == ["feat/x"]

    def test_missing_branch_leaves_only_the_head_ref(self):
        # A deleted or unreadable source branch must not append an empty ref —
        # `git fetch origin ""` is an error, not a no-op.
        assert GitHubReview(_PR(""), 5).head_refspecs() == ["pull/5/head"]
        assert GitLabReview(_MR(""), 5).head_refspecs() == []

    def test_base_default_is_the_source_branch(self):
        class _Bare(Review):
            def metadata(self): return ReviewMetadata(
                number=1, title="t", author="a",
                source_branch="topic", target_branch="main",
            )
            def diffs(self): ...
            def discussions(self): ...
            def _post_one(self, comment): ...

        assert _Bare().head_refspecs() == ["topic"]


# --- the CLI actually walks the candidates -----------------------------------

class _FakeReview:
    """Stands in for an opened review: a fork PR whose branch is not on origin."""

    def __init__(self, head_sha, refspecs=("pull/17/head", "schema-cache-staleness")):
        self._head = head_sha
        self._refspecs = refspecs
        self.refspec_calls = 0

    def metadata(self):
        return ReviewMetadata(number=17, title="t", author="a",
                              source_branch="schema-cache-staleness", target_branch="main")

    def diffs(self):
        return type("_D", (), {"shas": {"head_sha": self._head}})()

    def head_refspecs(self, meta=None):
        self.refspec_calls += 1
        return list(self._refspecs)


def _wire(monkeypatch, *, resolves_on: str | None, fetched: list[str],
          present: bool = False, refspecs=("pull/17/head", "schema-cache-staleness")):
    """Patch the CLI's git seam. The head appears only after `resolves_on`."""
    state = {"present": present}
    review = _FakeReview("deadbeef", refspecs=refspecs)

    # `worktree` does `from myopic.platforms.base import open_review` inside the
    # function body, so the name resolves against base_mod at call time —
    # patching cli_mod would be inert.
    import myopic.platforms.base as base_mod
    monkeypatch.setattr(base_mod, "open_review", lambda url: review)

    def _fetch(root, ref):
        fetched.append(ref)
        if ref == resolves_on:
            state["present"] = True
        return state["present"]

    monkeypatch.setattr(cli_mod.gitutil, "commit_present", lambda root, sha: state["present"])
    monkeypatch.setattr(cli_mod.gitutil, "fetch_ref", _fetch)
    monkeypatch.setattr(cli_mod.gitutil, "add_worktree", lambda root, path, ref: True)
    return review


class TestWorktreeFetchFallthrough:
    def test_fork_pr_resolves_via_pull_head(self, tmp_path, monkeypatch):
        fetched: list[str] = []
        _wire(monkeypatch, resolves_on="pull/17/head", fetched=fetched)
        result = CliRunner().invoke(
            cli_mod.cli,
            ["worktree", "https://github.com/o/r/pull/17", str(tmp_path),
             "--path", str(tmp_path / "wt")],
        )
        assert result.exit_code == 0, result.output
        # Stops at the first ref that works — the branch is never attempted.
        assert fetched == ["pull/17/head"]

    def test_falls_back_to_branch_when_head_ref_unavailable(self, tmp_path, monkeypatch):
        fetched: list[str] = []
        _wire(monkeypatch, resolves_on="schema-cache-staleness", fetched=fetched)
        result = CliRunner().invoke(
            cli_mod.cli,
            ["worktree", "https://github.com/o/r/pull/17", str(tmp_path),
             "--path", str(tmp_path / "wt")],
        )
        assert result.exit_code == 0, result.output
        assert fetched == ["pull/17/head", "schema-cache-staleness"]

    def test_head_already_local_skips_fetch_and_refspec_lookup(self, tmp_path, monkeypatch):
        # Building refspecs can cost a platform round trip (GitLab's metadata()
        # lists the MR's commits), so it must not happen when nothing is fetched.
        fetched: list[str] = []
        review = _wire(monkeypatch, resolves_on=None, fetched=fetched, present=True)
        result = CliRunner().invoke(
            cli_mod.cli,
            ["worktree", "https://github.com/o/r/pull/17", str(tmp_path),
             "--path", str(tmp_path / "wt")],
        )
        assert result.exit_code == 0, result.output
        assert fetched == []
        assert review.refspec_calls == 0

    def test_no_candidate_refs_does_not_claim_a_fetch_happened(self, tmp_path, monkeypatch):
        # An empty refspec list means nothing was fetched; saying "after
        # fetching" would point the reader at a network fault that never was.
        fetched: list[str] = []
        _wire(monkeypatch, resolves_on=None, fetched=fetched, refspecs=())
        result = CliRunner().invoke(
            cli_mod.cli,
            ["worktree", "https://github.com/o/r/pull/17", str(tmp_path),
             "--path", str(tmp_path / "wt")],
        )
        assert result.exit_code == 1
        assert fetched == []
        assert "after fetching" not in result.output
        assert "source branch" in result.output

    def test_exhausting_candidates_reports_what_was_tried(self, tmp_path, monkeypatch):
        fetched: list[str] = []
        _wire(monkeypatch, resolves_on=None, fetched=fetched)
        result = CliRunner().invoke(
            cli_mod.cli,
            ["worktree", "https://github.com/o/r/pull/17", str(tmp_path),
             "--path", str(tmp_path / "wt")],
        )
        assert result.exit_code == 1
        assert fetched == ["pull/17/head", "schema-cache-staleness"]
        assert "pull/17/head" in result.output and "schema-cache-staleness" in result.output
