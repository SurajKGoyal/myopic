"""
Tests for mr_verify_review — the platform-neutral join of discussions and diff.
A fake Review supplies crafted DiscussionSet / DiffSet so we assert the
nearby-change pairing, windowing, de-duplication, and located/unlocated split.
"""

from __future__ import annotations

from myopic.platforms.base import (
    DiffSet,
    Discussion,
    DiscussionSet,
    FileDiff,
    Note,
    Review,
    ReviewMetadata,
)
from myopic.tools.verify_review import mr_verify_review

_PATCH_A = (
    "@@ -10,3 +10,4 @@\n"
    " context\n"
    "-old at 11\n"
    "+new at 11\n"
    "+added at 12\n"
    " more\n"
)
# A change far from where the b.py thread sits (line 500).
_PATCH_B = "@@ -1,2 +1,2 @@\n-a\n+b\n context\n"


class _FakeReview(Review):
    platform_name = "fake"

    def __init__(self, discussions, general=None) -> None:
        self._disc = DiscussionSet(discussions=discussions, general_comments=general or [])

    def metadata(self):
        return ReviewMetadata(number=42, title="A PR", author="me",
                              source_branch="f", target_branch="main")

    def diffs(self):
        return DiffSet(files=[
            FileDiff(file_path="a.py", old_path="a.py", patch=_PATCH_A),
            FileDiff(file_path="b.py", old_path="b.py", patch=_PATCH_B),
        ])

    def discussions(self):
        return self._disc

    def _post_one(self, comment):  # pragma: no cover - unused
        raise NotImplementedError


def _note(body="fix this", author="rev", system=False):
    return Note(author=author, body=body, system=system)


def _run(monkeypatch, discussions, general=None, window=40):
    review = _FakeReview(discussions, general)
    monkeypatch.setattr("myopic.tools.verify_review.open_review", lambda url: review)
    return mr_verify_review("u", window=window)


def test_thread_with_nearby_changes(monkeypatch):
    disc = [Discussion(id="d1", resolved=False, file_path="a.py", line=11, notes=[_note()])]
    out = _run(monkeypatch, disc)
    assert out["mr_number"] == 42
    t = out["threads"][0]
    assert t["has_changes"] is True
    # del@11, add@11, add@12 all within the window, sorted by distance
    assert [c["distance"] for c in t["nearby_changes"]] == [0, 0, 1]
    assert out["summary"]["threads_with_changes"] == 1


def test_thread_without_nearby_changes(monkeypatch):
    disc = [Discussion(id="d1", resolved=False, file_path="b.py", line=500, notes=[_note()])]
    out = _run(monkeypatch, disc)
    assert out["threads"][0]["has_changes"] is False
    assert out["summary"]["threads_without_changes"] == 1


def test_window_excludes_distant_changes(monkeypatch):
    disc = [Discussion(id="d1", resolved=False, file_path="a.py", line=11, notes=[_note()])]
    out = _run(monkeypatch, disc, window=0)   # only exact-distance-0 lines
    dists = [c["distance"] for c in out["threads"][0]["nearby_changes"]]
    assert dists == [0, 0]                      # the add@12 (distance 1) is dropped


def test_dedup_identical_threads(monkeypatch):
    disc = [
        Discussion(id="d1", resolved=False, file_path="a.py", line=11, notes=[_note("same")]),
        Discussion(id="d2", resolved=False, file_path="a.py", line=11, notes=[_note("same")]),
    ]
    out = _run(monkeypatch, disc)
    assert out["summary"]["total_threads"] == 1


def test_replies_and_resolved_counts(monkeypatch):
    disc = [Discussion(id="d1", resolved=True, file_path="a.py", line=11, notes=[
        _note("first", author="rev"),
        _note("reply", author="author"),
    ])]
    out = _run(monkeypatch, disc)
    t = out["threads"][0]
    assert t["first_comment"] == {"author": "rev", "body": "first"}
    assert t["replies"] == [{"author": "author", "body": "reply"}]
    assert out["summary"]["resolved"] == 1 and out["summary"]["unresolved"] == 0


def test_unlocated_thread_split(monkeypatch):
    disc = [Discussion(id="d1", resolved=False, file_path=None, line=None, notes=[_note("general")])]
    out = _run(monkeypatch, disc)
    assert out["threads"] == []
    assert len(out["threads_no_location"]) == 1
    assert out["threads_no_location"][0]["first_comment"]["body"] == "general"


def test_system_notes_ignored_for_first_comment(monkeypatch):
    disc = [Discussion(id="d1", resolved=False, file_path="a.py", line=11, notes=[
        _note("changed the line", author="system", system=True),
        _note("real comment", author="rev"),
    ])]
    out = _run(monkeypatch, disc)
    assert out["threads"][0]["first_comment"]["body"] == "real comment"
