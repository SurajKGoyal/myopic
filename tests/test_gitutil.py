"""
Tests for the git freshness helpers. Uses a real throwaway git repo; each
helper must degrade to a safe None/False on a non-git directory.
"""

from __future__ import annotations

import subprocess

import pytest

from myopic import gitutil


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "--allow-empty", "-q", "-m", "first")


@pytest.fixture
def repo(tmp_path):
    _init_repo(tmp_path)
    return tmp_path


class TestNonGit:
    def test_head_sha_none(self, tmp_path):
        assert gitutil.head_sha(str(tmp_path)) is None

    def test_is_dirty_false(self, tmp_path):
        assert gitutil.is_dirty(str(tmp_path)) is False

    def test_is_git_repo_false(self, tmp_path):
        assert gitutil.is_git_repo(str(tmp_path)) is False

    def test_commits_behind_none(self, tmp_path):
        assert gitutil.commits_behind(str(tmp_path), "deadbeef") is None


class TestGitRepo:
    def test_head_sha(self, repo):
        sha = gitutil.head_sha(str(repo))
        assert sha and len(sha) == 40

    def test_is_git_repo(self, repo):
        assert gitutil.is_git_repo(str(repo)) is True

    def test_dirty_detection(self, repo):
        assert gitutil.is_dirty(str(repo)) is False
        (repo / "new.txt").write_text("x", encoding="utf-8")
        assert gitutil.is_dirty(str(repo)) is True

    def test_commits_behind_counts(self, repo):
        first = gitutil.head_sha(str(repo))
        assert gitutil.commits_behind(str(repo), first) == 0
        _git(repo, "commit", "--allow-empty", "-q", "-m", "second")
        _git(repo, "commit", "--allow-empty", "-q", "-m", "third")
        assert gitutil.commits_behind(str(repo), first) == 2

    def test_commits_behind_unknown_sha(self, repo):
        assert gitutil.commits_behind(str(repo), "0" * 40) is None

    def test_short(self):
        assert gitutil.short("abcdef1234567890") == "abcdef12"
        assert gitutil.short(None) is None

    def test_commit_present(self, repo):
        sha = gitutil.head_sha(str(repo))
        assert gitutil.commit_present(str(repo), sha) is True
        assert gitutil.commit_present(str(repo), "0" * 40) is False
        assert gitutil.commit_present(str(repo), "") is False

    def test_add_worktree(self, repo, tmp_path):
        (repo / "f.txt").write_text("hi", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "add f")
        sha = gitutil.head_sha(str(repo))

        wt = tmp_path / "wt"
        assert gitutil.add_worktree(str(repo), str(wt), sha) is True
        assert (wt / "f.txt").read_text() == "hi"

    def test_add_worktree_bad_ref(self, repo, tmp_path):
        assert gitutil.add_worktree(str(repo), str(tmp_path / "wt2"), "0" * 40) is False


class TestNonGitPrimitives:
    def test_commit_present_non_git(self, tmp_path):
        assert gitutil.commit_present(str(tmp_path), "0" * 40) is False

    def test_fetch_ref_non_git(self, tmp_path):
        # No remote / not a repo → False, never raises.
        assert gitutil.fetch_ref(str(tmp_path), "main") is False
