"""
Tests for the mr_review_context guard that verifies `root` actually holds the
MR's code. gitutil is monkeypatched so no real repo state is needed — the guard
just consumes (is_git_repo, head_sha, commit_present).
"""

from __future__ import annotations

import importlib.util

import pytest

import myopic.tools.review_context as rc
from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata

_LANCEDB = importlib.util.find_spec("lancedb") is not None


class _FakeReview:
    def __init__(self, head: str):
        self._head = head

    def metadata(self):
        return ReviewMetadata(68, "t", "a", "feat/x", "main")

    def diffs(self):
        return DiffSet(
            files=[FileDiff("a.py", "a.py", new_file=True, patch="@@ -0,0 +1 @@\n+x = 1\n")],
            shas={"head_sha": self._head, "base_sha": "base", "start_sha": "base"},
        )


def _run(monkeypatch, tmp_path, head, *, is_git=True, present=True, root_sha=None):
    monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))  # keep any index probe hermetic
    monkeypatch.setenv("MYOPIC_AUTO_INDEX", "0")      # test prompts, not auto-index
    monkeypatch.setattr(rc, "open_review", lambda url: _FakeReview(head))
    monkeypatch.setattr(rc, "dependency_impact", lambda sym, root: {"symbol": sym})
    monkeypatch.setattr(rc.gitutil, "is_git_repo", lambda r: is_git)
    monkeypatch.setattr(rc.gitutil, "head_sha", lambda r: head if root_sha is None else root_sha)
    monkeypatch.setattr(rc.gitutil, "commit_present", lambda r, s: present)
    return rc.mr_review_context("https://gitlab.com/g/p/-/merge_requests/68", str(tmp_path))


def test_on_mr_head_no_warning(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "abc123", present=True, root_sha="abc123")
    assert out["root_status"]["ok"] is True
    assert out["root_status"]["state"] == "on_mr_head"
    assert "warning" not in out


def test_mr_head_absent_warns(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "abc123", present=False, root_sha="def456")
    assert out["root_status"]["ok"] is False
    assert out["root_status"]["state"] == "mr_head_absent"
    assert "worktree" in out["warning"]
    assert "MISSING" in out["warning"]


def test_present_but_not_checked_out(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "abc123", present=True, root_sha="def456")
    assert out["root_status"]["state"] == "not_checked_out"
    assert "warning" in out
    assert "MISSING" not in out["warning"]   # softer message


def test_non_git_root_skips_guard(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "abc123", is_git=False)
    assert "root_status" not in out
    assert "warning" not in out


@pytest.mark.skipif(not _LANCEDB, reason="index_state needs the semantic extra")
class TestIndexPrompts:
    """The absent-index prompt, and its suppression when root is the wrong branch."""

    def test_absent_index_prompts_to_index(self, monkeypatch, tmp_path):
        # root holds the MR (ok), but the repo was never indexed → offer to index.
        out = _run(monkeypatch, tmp_path, "abc", present=True, root_sha="abc")
        assert out["root_status"]["ok"] is True
        assert out.get("index_status", {}).get("state") == "absent"
        assert "next" in out and "index_repo" in out["next"]

    def test_bad_root_suppresses_index_prompt(self, monkeypatch, tmp_path):
        # root does NOT hold the MR → warning to fix the checkout first; do NOT
        # also prompt to index (it would index the wrong branch).
        out = _run(monkeypatch, tmp_path, "abc", present=False, root_sha="def")
        assert "warning" in out
        assert "next" not in out


@pytest.mark.skipif(not _LANCEDB, reason="auto-index needs the semantic extra")
class TestAutoIndex:
    def test_auto_indexes_on_first_review(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        home.mkdir()
        repo.mkdir()
        (repo / "pay.py").write_text("def process_payment(x):\n    return x\n", encoding="utf-8")

        monkeypatch.setenv("MYOPIC_HOME", str(home))
        monkeypatch.delenv("MYOPIC_AUTO_INDEX", raising=False)   # default ON

        fake = lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        monkeypatch.setattr("myopic.embeddings.embed_texts", fake)
        monkeypatch.setattr("myopic.semantic.indexer.embed_texts", fake)

        monkeypatch.setattr(rc, "open_review", lambda url: _FakeReview("abc"))
        monkeypatch.setattr(rc, "dependency_impact", lambda sym, root: {"symbol": sym})
        monkeypatch.setattr(rc.gitutil, "is_git_repo", lambda r: True)
        monkeypatch.setattr(rc.gitutil, "head_sha", lambda r: "abc")
        monkeypatch.setattr(rc.gitutil, "commit_present", lambda r, s: True)

        out = rc.mr_review_context("https://gitlab.com/g/p/-/merge_requests/1", str(repo))
        # It indexed itself on the first review → semantic is now available,
        # and there's no "run index_repo" prompt left over.
        assert out["semantic_available"] is True
        assert "index_repo" not in out.get("next", "")

    def test_opt_out_leaves_it_absent(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        home.mkdir()
        repo.mkdir()
        (repo / "pay.py").write_text("def process_payment(x):\n    return x\n", encoding="utf-8")

        monkeypatch.setenv("MYOPIC_HOME", str(home))
        monkeypatch.setenv("MYOPIC_AUTO_INDEX", "0")            # opted out

        monkeypatch.setattr(rc, "open_review", lambda url: _FakeReview("abc"))
        monkeypatch.setattr(rc, "dependency_impact", lambda sym, root: {"symbol": sym})
        monkeypatch.setattr(rc.gitutil, "is_git_repo", lambda r: True)
        monkeypatch.setattr(rc.gitutil, "head_sha", lambda r: "abc")
        monkeypatch.setattr(rc.gitutil, "commit_present", lambda r, s: True)

        out = rc.mr_review_context("https://gitlab.com/g/p/-/merge_requests/1", str(repo))
        assert out["semantic_available"] is False
        assert out.get("index_status", {}).get("state") == "absent"
