"""
Tests for the mr_review_context guard that verifies `root` actually holds the
MR's code. gitutil is monkeypatched so no real repo state is needed — the guard
just consumes (is_git_repo, head_sha, commit_present).
"""

from __future__ import annotations

import myopic.tools.review_context as rc
from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata


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
