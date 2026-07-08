"""
Tool-level tests for mr_diff_sections — AST/symbol-grouped diff, the same
'fits too large' safety layer as mr_diff_lines but grouped by symbol.
"""

from __future__ import annotations

import pytest

import myopic.tools.diff_sections as diff_sections_mod
from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata


class _FakeReview:
    def __init__(self, files: list[FileDiff]):
        self._files = files

    def metadata(self) -> ReviewMetadata:
        return ReviewMetadata(
            number=42, title="Big MR", author="alice",
            source_branch="feat/big", target_branch="main", commits=["c1"],
        )

    def diffs(self) -> DiffSet:
        return DiffSet(files=self._files, shas={"base_sha": "b", "head_sha": "h"})


def _new_file_patch() -> str:
    body = "\n".join(f"+    print({i})" for i in range(3))
    return f"@@ -0,0 +1,4 @@\n+def greet():\n{body}\n"


def _modified_patch() -> str:
    return (
        "@@ -1,4 +1,5 @@ def existing_function():\n"
        " def existing_function():\n"
        "     x = 1\n"
        "+    y = 2\n"
        "     return x\n"
    )


def _padding_patch(n_added: int) -> str:
    body = "\n".join(f"+    padding_line_{i}_" + ("x" * 40) for i in range(n_added))
    return f"@@ -0,0 +1,{n_added} @@\n{body}"


def _file(path: str, patch: str, new_file: bool = False) -> FileDiff:
    return FileDiff(file_path=path, old_path=path, new_file=new_file, patch=patch)


def _install(monkeypatch, files):
    review = _FakeReview(files)
    monkeypatch.setattr(diff_sections_mod, "open_review", lambda url: review)


class TestDiffSectionsBasic:
    def test_new_file_and_modified_file_produce_sections(self, monkeypatch):
        files = [
            _file("src/greet.py", _new_file_patch(), new_file=True),
            _file("src/existing.py", _modified_patch(), new_file=False),
        ]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections("http://x/mr/42")

        assert "error" not in res
        assert res["mr_number"] == 42
        by_path = {f["file_path"]: f for f in res["files"]}
        assert "src/greet.py" in by_path
        assert "src/existing.py" in by_path

        greet = by_path["src/greet.py"]
        assert greet["new_file"] is True
        assert greet["language"] == "python"
        assert len(greet["sections"]) >= 1

        existing = by_path["src/existing.py"]
        assert len(existing["sections"]) >= 1
        # Symbol resolved from the hunk header hint.
        assert any(s["symbol"] == "existing_function" for s in existing["sections"])

    def test_all_changed_lines_present_in_sections(self, monkeypatch):
        files = [_file("src/existing.py", _modified_patch(), new_file=False)]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections("http://x/mr/42")

        sections = res["files"][0]["sections"]
        all_changes = [c for s in sections for c in s["changes"]]
        # One add ("y = 2") — context lines excluded by default.
        assert any(c["type"] == "add" and "y = 2" in c["content"] for c in all_changes)
        assert all(c["type"] != "context" for c in all_changes)

    def test_include_context_lines(self, monkeypatch):
        files = [_file("src/existing.py", _modified_patch(), new_file=False)]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections(
            "http://x/mr/42", include_context_lines=True,
        )

        sections = res["files"][0]["sections"]
        all_changes = [c for s in sections for c in s["changes"]]
        assert any(c["type"] == "context" for c in all_changes)


class TestDiffSectionsBudgetAndNoise:
    def test_budget_truncates_and_reports(self, monkeypatch):
        files = [_file(f"src/file{i}.py", _padding_patch(120), new_file=True) for i in range(10)]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections("http://x/mr/42", max_chars=6000)

        assert res["truncated"] is True
        assert len(res["files"]) >= 1
        assert len(res["omitted_files"]) > 0
        total = (res["stats"]["files_returned"]
                 + res["stats"]["files_omitted"]
                 + res["stats"]["files_skipped"])
        assert total == 10
        assert "next" in res and "files_filter" in res["next"]

    def test_noise_skipped_not_expanded(self, monkeypatch):
        files = [
            _file("src/real.py", _new_file_patch(), new_file=True),
            _file("package-lock.json", _padding_patch(4000), new_file=True),
            _file("assets/logo.png", _padding_patch(10), new_file=True),
        ]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections("http://x/mr/42")

        returned = {f["file_path"] for f in res["files"]}
        skipped = {f["file_path"] for f in res["skipped_files"]}
        assert returned == {"src/real.py"}
        assert skipped == {"package-lock.json", "assets/logo.png"}
        assert res["truncated"] is False

    def test_targeted_fetch_bypasses_guards(self, monkeypatch):
        files = [
            _file("src/real.py", _new_file_patch(), new_file=True),
            _file("package-lock.json", _padding_patch(50), new_file=True),
        ]
        _install(monkeypatch, files)

        res = diff_sections_mod.mr_diff_sections(
            "http://x/mr/42", files_filter=["package-lock.json"], max_chars=10,
        )

        returned = {f["file_path"] for f in res["files"]}
        assert "package-lock.json" in returned  # targeted overrides noise-skip
        assert res["truncated"] is False          # budget disabled when targeted
        assert res["skipped_files"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestReviewContextNudge:
    def test_points_to_review_context(self, monkeypatch):
        _install(monkeypatch, [_file("src/greet.py", _new_file_patch(), new_file=True)])
        out = diff_sections_mod.mr_diff_sections("u")
        assert out["truncated"] is False
        assert "mr_review_context" in out["next"]

    def test_truncated_keeps_both_hints(self, monkeypatch):
        files = [_file(f"src/file{i}.py", _padding_patch(120), new_file=True) for i in range(10)]
        _install(monkeypatch, files)
        out = diff_sections_mod.mr_diff_sections("u", max_chars=500)
        assert out["truncated"] is True
        assert "files_filter" in out["next"] and "mr_review_context" in out["next"]
