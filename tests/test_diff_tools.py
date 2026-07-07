"""
Tool-level tests for mr_changed_files and mr_diff_lines — the 'fill too large'
safety layer. A fake review supplies synthetic diffs, so no network is needed.
"""

from __future__ import annotations

import pytest

from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata
import myopic.tools.changed_files as changed_files_mod
import myopic.tools.diff_lines as diff_lines_mod


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


def _patch(n_added: int) -> str:
    body = "\n".join(f"+    code line {i}" for i in range(n_added))
    return f"@@ -0,0 +1,{n_added} @@\n{body}"


def _file(path: str, added: int) -> FileDiff:
    return FileDiff(file_path=path, old_path=path, new_file=True, patch=_patch(added))


def _install(monkeypatch, files):
    review = _FakeReview(files)
    monkeypatch.setattr(changed_files_mod, "open_review", lambda url: review)
    monkeypatch.setattr(diff_lines_mod, "open_review", lambda url: review)


# ---------------------------------------------------------------------------
# mr_changed_files — the manifest
# ---------------------------------------------------------------------------

class TestChangedFiles:
    def test_manifest_classifies_and_orders(self, monkeypatch):
        files = [
            _file("package-lock.json", 4000),
            _file("src/small.py", 5),
            _file("src/big.py", 200),
        ]
        _install(monkeypatch, files)

        res = changed_files_mod.mr_changed_files("http://x/mr/42")

        # No diff content in a manifest.
        assert all("hunks" not in f for f in res["files"])
        # Reviewable first, largest-change first -> big.py before small.py; lockfile last.
        order = [f["file_path"] for f in res["files"]]
        assert order == ["src/big.py", "src/small.py", "package-lock.json"]
        lock = res["files"][-1]
        assert lock["reviewable"] is False and lock["skip_reason"] == "lockfile"
        assert res["stats"]["reviewable_files"] == 2
        assert res["stats"]["skipped_files"] == 1


# ---------------------------------------------------------------------------
# mr_diff_lines — budget + noise
# ---------------------------------------------------------------------------

class TestDiffLinesBudget:
    def test_budget_truncates_and_reports(self, monkeypatch):
        files = [_file(f"src/file{i}.py", 120) for i in range(10)]
        _install(monkeypatch, files)

        res = diff_lines_mod.mr_diff_lines("http://x/mr/42", max_chars=6000)

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
            _file("src/real.py", 30),
            _file("package-lock.json", 4000),
            _file("assets/logo.png", 10),
        ]
        _install(monkeypatch, files)

        res = diff_lines_mod.mr_diff_lines("http://x/mr/42")

        returned = {f["file_path"] for f in res["files"]}
        skipped = {f["file_path"] for f in res["skipped_files"]}
        assert returned == {"src/real.py"}
        assert skipped == {"package-lock.json", "assets/logo.png"}
        assert res["truncated"] is False

    def test_targeted_fetch_bypasses_guards(self, monkeypatch):
        files = [
            _file("src/real.py", 30),
            _file("package-lock.json", 50),
        ]
        _install(monkeypatch, files)

        res = diff_lines_mod.mr_diff_lines(
            "http://x/mr/42", files_filter=["package-lock.json"], max_chars=10,
        )

        returned = {f["file_path"] for f in res["files"]}
        assert "package-lock.json" in returned  # targeted overrides noise-skip
        assert res["truncated"] is False         # budget disabled when targeted
        assert res["skipped_files"] == []

    def test_lines_filter_is_compact(self, monkeypatch):
        _install(monkeypatch, [_file("src/real.py", 5)])
        res = diff_lines_mod.mr_diff_lines(
            "http://x/mr/42", lines_filter={"real.py": [2]},
        )
        assert "line_mappings" in res["files"][0]
        assert "hunks" not in res["files"][0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
