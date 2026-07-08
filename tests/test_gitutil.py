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


    def test_common_dir_shared_by_clone_and_worktree(self, repo, tmp_path):
        (repo / "f.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "c")
        sha = gitutil.head_sha(str(repo))
        wt = tmp_path / "wt"
        gitutil.add_worktree(str(repo), str(wt), sha)

        cd_main = gitutil.common_dir(str(repo))
        cd_wt = gitutil.common_dir(str(wt))
        assert cd_main and cd_wt
        assert cd_main == cd_wt   # clone and worktree → one repo identity


class TestNonGitPrimitives:
    def test_commit_present_non_git(self, tmp_path):
        assert gitutil.commit_present(str(tmp_path), "0" * 40) is False

    def test_fetch_ref_non_git(self, tmp_path):
        # No remote / not a repo → False, never raises.
        assert gitutil.fetch_ref(str(tmp_path), "main") is False

    def test_common_dir_non_git(self, tmp_path):
        assert gitutil.common_dir(str(tmp_path)) is None


class TestIndexKey:
    """The semantic index must key by repository, not checkout path."""

    def test_clone_and_worktree_share_one_index(self, repo, tmp_path):
        from myopic.semantic import store

        (repo / "f.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "c")
        sha = gitutil.head_sha(str(repo))
        wt = tmp_path / "wt"
        gitutil.add_worktree(str(repo), str(wt), sha)

        assert store._table_name(str(repo)) == store._table_name(str(wt))

    def test_different_repos_differ(self, tmp_path):
        from myopic.semantic import store

        a = tmp_path / "a"
        b = tmp_path / "b"
        for d in (a, b):
            d.mkdir()
            _init_repo(d)
        assert store._table_name(str(a)) != store._table_name(str(b))

    def test_non_git_falls_back_to_path(self, tmp_path):
        from myopic.semantic import store

        a = tmp_path / "plain"
        a.mkdir()
        # deterministic + path-based when not a git repo
        assert store._table_name(str(a)) == store._table_name(str(a))
