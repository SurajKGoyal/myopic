"""
Tests for mr_post_comments — validation, the shared queue+backoff driver, and
each backend's single-comment positioning. No network: fake SDK objects and a
patched sleep drive everything.
"""

from __future__ import annotations

import pytest

from myopic.platforms import base as base_mod
from myopic.platforms.base import (
    CommentDraft,
    DiffSet,
    DiscussionSet,
    Review,
    ReviewMetadata,
)
from myopic.tools.post_comments import mr_post_comments


# --- a fake Review that scripts _post_one outcomes per file -----------------

class _HttpError(Exception):
    """Mimics a platform SDK error carrying an HTTP status."""
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"http {status}")


class _ScriptedReview(Review):
    platform_name = "fake"

    def __init__(self, script: dict[str, list]) -> None:
        # script: file_path -> list of outcomes, one per attempt. An Exception is
        # raised; None means success.
        self._script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def metadata(self):  # pragma: no cover - unused here
        return ReviewMetadata(1, "t", "a", "s", "m")

    def diffs(self):  # pragma: no cover - unused here
        return DiffSet()

    def discussions(self):  # pragma: no cover - unused here
        return DiscussionSet()

    def _post_one(self, comment: CommentDraft) -> None:
        self.calls.append(comment.file_path)
        outcome = self._script[comment.file_path].pop(0)
        if isinstance(outcome, Exception):
            raise outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually sleep; record how often backoff was invoked."""
    slept: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def _draft(path="a.py", line=5):
    return CommentDraft(file_path=path, body="b", new_line=line)


# --- driver: retry / fail-fast / partial ------------------------------------

class TestBackoffDriver:
    def test_retries_transient_then_succeeds(self, _no_sleep):
        review = _ScriptedReview({"a.py": [_HttpError(429), _HttpError(503), None]})
        res = review.post_comments([_draft()])
        assert res.posted == 1 and res.failed == 0
        assert review.calls == ["a.py", "a.py", "a.py"]   # 2 retries + success
        assert len(_no_sleep) == 2                         # backoff between attempts

    def test_backoff_is_exponential(self, _no_sleep):
        review = _ScriptedReview({"a.py": [_HttpError(429), _HttpError(429), None]})
        review.post_comments([_draft()], base_delay=0.5, gap=0)
        assert _no_sleep == [0.5, 1.0]                     # doubles each retry

    def test_permanent_error_fails_fast(self, _no_sleep):
        review = _ScriptedReview({"a.py": [_HttpError(422)]})
        res = review.post_comments([_draft()])
        assert res.failed == 1 and res.posted == 0
        assert review.calls == ["a.py"]                    # no retry on 422
        assert _no_sleep == []
        assert res.details[0].status == "failed"
        assert "422" in res.details[0].error

    def test_exhausts_retries(self, _no_sleep):
        review = _ScriptedReview({"a.py": [_HttpError(429)] * 5})
        res = review.post_comments([_draft()], max_retries=2, gap=0)
        assert res.failed == 1
        assert review.calls == ["a.py"] * 3                # attempts 0,1,2
        assert len(_no_sleep) == 2

    def test_partial_success_preserves_order(self, _no_sleep):
        review = _ScriptedReview({
            "a.py": [None],
            "b.py": [_HttpError(422)],
            "c.py": [None],
        })
        comments = [_draft("a.py"), _draft("b.py"), _draft("c.py")]
        res = review.post_comments(comments, gap=0)
        assert (res.total, res.posted, res.failed) == (3, 2, 1)
        assert [d.file_path for d in res.details] == ["a.py", "b.py", "c.py"]
        assert [d.status for d in res.details] == ["posted", "failed", "posted"]
        assert res.published is True


# --- tool-layer validation ---------------------------------------------------

class TestValidation:
    def test_empty(self):
        assert "error" in mr_post_comments("u", [])

    def test_too_many(self):
        many = [{"file_path": "a", "body": "b", "new_line": 1}] * 3
        assert "Too many" in mr_post_comments("u", many, max_comments=2)["error"]

    def test_missing_file_path(self):
        assert "file_path" in mr_post_comments("u", [{"body": "b", "new_line": 1}])["error"]

    def test_missing_body(self):
        assert "body" in mr_post_comments("u", [{"file_path": "a", "new_line": 1}])["error"]

    def test_missing_line(self):
        out = mr_post_comments("u", [{"file_path": "a", "body": "b"}])
        assert "new_line" in out["error"]


def test_tool_passes_through_result(monkeypatch):
    review = _ScriptedReview({"a.py": [None]})
    monkeypatch.setattr("myopic.tools.post_comments.open_review", lambda url: review)
    out = mr_post_comments("u", [{"file_path": "a.py", "body": "b", "new_line": 5}])
    assert out["platform"] == "fake"
    assert out["posted"] == 1 and out["failed"] == 0
    assert out["details"][0] == {
        "file_path": "a.py", "line": 5, "status": "posted", "error": None,
    }


# --- backend positioning -----------------------------------------------------

class TestGitLabPosting:
    def test_position_payload(self, monkeypatch):
        from myopic.platforms.gitlab import GitLabReview

        captured = {}

        class _Discussions:
            def create(self, payload):
                captured.update(payload)

        class _MR:
            discussions = _Discussions()

        review = GitLabReview(_MR(), 1)
        monkeypatch.setattr(review, "diffs", lambda: DiffSet(shas={
            "base_sha": "B", "head_sha": "H", "start_sha": "S",
        }))
        review._post_one(CommentDraft("src/x.py", "please fix", new_line=12))

        assert captured["body"] == "please fix"
        pos = captured["position"]
        assert pos["base_sha"] == "B" and pos["head_sha"] == "H" and pos["start_sha"] == "S"
        assert pos["new_path"] == "src/x.py" and pos["new_line"] == 12
        assert "old_line" not in pos

    def test_missing_shas_raises(self, monkeypatch):
        from myopic.platforms.gitlab import GitLabReview
        review = GitLabReview(object(), 1)
        monkeypatch.setattr(review, "diffs", lambda: DiffSet(shas=None))
        with pytest.raises(RuntimeError, match="SHAs"):
            review._post_one(CommentDraft("a.py", "b", new_line=1))


class TestGitHubPosting:
    def _review(self, capture):
        from myopic.platforms.github import GitHubReview

        class _Head:
            sha = "HEADSHA"

        class _PR:
            head = _Head()
            def create_review_comment(self, **kwargs):
                capture.update(kwargs)

        return GitHubReview(_PR(), 7, token="secret")

    def test_new_line_is_right_side(self):
        cap = {}
        self._review(cap)._post_one(CommentDraft("a.py", "nit", new_line=9))
        assert cap["side"] == "RIGHT" and cap["line"] == 9
        assert cap["commit"] == "HEADSHA" and cap["path"] == "a.py"

    def test_old_line_is_left_side(self):
        cap = {}
        self._review(cap)._post_one(CommentDraft("a.py", "nit", old_line=4))
        assert cap["side"] == "LEFT" and cap["line"] == 4

    def test_error_scrubs_token(self):
        from myopic.platforms.github import GitHubReview
        review = GitHubReview(object(), 7, token="secret")
        assert review._scrub_error("failed with token secret here") == "failed with token *** here"
