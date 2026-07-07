"""
Tests for the GitHub platform adapter. No network — a fake PyGithub PR object
drives the mapping, so we assert the GitHub backend emits the exact same
normalized shapes (ReviewMetadata / DiffSet / DiscussionSet) the tools expect.
"""

from __future__ import annotations

import pytest

from myopic.platforms import base as base_mod
from myopic.platforms.base import open_review
from myopic.platforms.github import GitHubPlatform, GitHubReview
from myopic.platforms.gitlab import GitLabPlatform


# --- fake PyGithub objects ---------------------------------------------------

class _User:
    def __init__(self, login): self.login = login


class _Ref:
    def __init__(self, ref, sha): self.ref = ref; self.sha = sha


class _File:
    def __init__(self, filename, status, patch, previous_filename=None):
        self.filename = filename
        self.status = status
        self.patch = patch
        self.previous_filename = previous_filename


class _Commit:
    def __init__(self, message):
        self.commit = type("_C", (), {"message": message})()


class _ReviewComment:
    def __init__(self, cid, path, line, body, login, in_reply_to_id=None):
        self.id = cid
        self.path = path
        self.line = line
        self.original_line = line
        self.body = body
        self.user = _User(login)
        self.created_at = None
        self.in_reply_to_id = in_reply_to_id


class _IssueComment:
    def __init__(self, body, login):
        self.body = body
        self.user = _User(login)
        self.created_at = None


class _FakePR:
    title = "Add feature"
    body = "a description"
    state = "open"
    mergeable_state = "clean"

    def __init__(self):
        self.user = _User("octocat")
        self.head = _Ref("feat/x", "headsha123")
        self.base = _Ref("main", "basesha456")

    def get_commits(self):
        return [_Commit("first commit\n\nlong body"), _Commit("second commit")]

    def get_files(self):
        return [
            _File("src/app.py", "modified", "@@ -1 +1,2 @@\n+added line"),
            _File("brand_new.py", "added", "@@ -0,0 +1 @@\n+x = 1"),
            _File("gone.py", "removed", "@@ -1 +0,0 @@\n-old"),
            _File("dst.py", "renamed", "@@ -1 +1 @@\n-a\n+b", previous_filename="src.py"),
            _File("logo.png", "added", None),  # binary → patch is None
        ]

    def get_review_comments(self):
        return [
            _ReviewComment(1, "src/app.py", 5, "nit: rename this", "reviewer"),
            _ReviewComment(2, "src/app.py", 5, "done", "octocat", in_reply_to_id=1),
            _ReviewComment(3, "brand_new.py", 2, "add a test", "reviewer2"),
        ]

    def get_issue_comments(self):
        return [_IssueComment("LGTM overall", "manager")]


# --- metadata / diffs / discussions mapping ----------------------------------

class TestGitHubReviewMapping:
    def _review(self):
        return GitHubReview(_FakePR(), 42)

    def test_metadata(self):
        m = self._review().metadata()
        assert m.number == 42
        assert m.title == "Add feature"
        assert m.author == "octocat"
        assert m.source_branch == "feat/x"
        assert m.target_branch == "main"
        assert m.state == "open"
        assert m.merge_status == "clean"
        assert m.commits == ["first commit", "second commit"]  # first line only

    def test_diffs_shapes_and_flags(self):
        ds = self._review().diffs()
        assert ds.shas == {"base_sha": "basesha456", "head_sha": "headsha123", "start_sha": "basesha456"}
        by_path = {f.file_path: f for f in ds.files}
        assert by_path["brand_new.py"].new_file is True
        assert by_path["gone.py"].deleted_file is True
        assert by_path["dst.py"].renamed_file is True and by_path["dst.py"].old_path == "src.py"
        # binary file: patch omitted by GitHub -> normalized to empty string, not None
        assert by_path["logo.png"].patch == ""

    def test_discussions_group_threads_and_general(self):
        d = self._review().discussions()
        # comment 1 + its reply 2 -> one thread (2 notes); comment 3 -> another thread
        assert len(d.discussions) == 2
        first = next(x for x in d.discussions if x.file_path == "src/app.py")
        assert len(first.notes) == 2
        assert first.line == 5
        assert first.resolved is False  # GitHub REST doesn't expose thread resolution
        # issue comment -> general
        assert len(d.general_comments) == 1
        assert d.general_comments[0].author == "manager"


# --- URL handling / routing --------------------------------------------------

class TestRouting:
    def test_handles(self):
        assert GitHubPlatform.handles("https://github.com/o/r/pull/7") is True
        assert GitHubPlatform.handles("https://gitlab.com/g/p/-/merge_requests/7") is False

    def test_parse_url(self):
        repo, num = GitHubPlatform()._parse_url("https://github.com/octocat/Hello-World/pull/42")
        assert repo == "octocat/Hello-World"
        assert num == 42

    def test_parse_url_enterprise_host(self):
        repo, num = GitHubPlatform()._parse_url("https://ghe.corp.com/team/svc/pull/9")
        assert repo == "team/svc" and num == 9

    def test_open_review_routes_by_url(self, monkeypatch):
        monkeypatch.setattr(GitHubPlatform, "open", lambda self, url: ("github", url))
        monkeypatch.setattr(GitLabPlatform, "open", lambda self, url: ("gitlab", url))
        assert open_review("https://github.com/o/r/pull/1")[0] == "github"
        assert open_review("https://gitlab.com/g/p/-/merge_requests/2")[0] == "gitlab"

    def test_open_review_unknown_url_raises(self):
        with pytest.raises(ValueError, match="No registered platform"):
            open_review("https://example.com/not/a/review")


# --- config resolution -------------------------------------------------------

class TestGitHubConfig:
    def test_token_from_env_url_optional(self, tmp_path, monkeypatch):
        from myopic.config import load_config, invalidate_config_cache

        monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))  # no config.toml here
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_xyz")
        monkeypatch.delenv("MYOPIC_GITHUB_URL", raising=False)
        monkeypatch.delenv("GITHUB_URL", raising=False)
        invalidate_config_cache()

        cfg = load_config("github")
        assert cfg.platform == "github"
        assert cfg.token == "ghp_xyz"
        assert cfg.url == ""  # optional — public github.com

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        from myopic.config import load_config, invalidate_config_cache

        monkeypatch.setenv("MYOPIC_HOME", str(tmp_path))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("MYOPIC_GITHUB_TOKEN", raising=False)
        invalidate_config_cache()

        with pytest.raises(ValueError, match="GitHub"):
            load_config("github")
