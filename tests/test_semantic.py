"""
Hermetic tests for the myopic[semantic] feature.

No real Ollama server or LanceDB instance is needed — all external I/O is
monkeypatched. Three test classes cover:
  a) Graceful degradation when the optional extra is absent.
  b) Indexer chunk-collection and filtering logic.
  c) mr_review_context graph-first fusion with semantic unavailable.
"""

from __future__ import annotations

import pytest

from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata
import myopic.tools.index_repo as index_repo_mod
import myopic.tools.code_search as code_search_mod
import myopic.tools.review_context as review_context_mod
import myopic.semantic.indexer as indexer_mod


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeReview:
    """Minimal fake Review for monkeypatching open_review."""

    def __init__(self, files: list[FileDiff], mr_number: int = 99):
        self._files = files
        self._number = mr_number

    def metadata(self) -> ReviewMetadata:
        return ReviewMetadata(
            number=self._number,
            title="Semantic test MR",
            author="bob",
            source_branch="feat/semantic",
            target_branch="main",
            commits=["abc123"],
        )

    def diffs(self) -> DiffSet:
        return DiffSet(files=self._files, shas={"base_sha": "b", "head_sha": "h"})


def _make_patch(symbol: str) -> str:
    """Build a minimal unified-diff patch that mentions *symbol* in an added line."""
    return f"@@ -0,0 +1,3 @@\n+def {symbol}():\n+    pass\n+    return True\n"


# ---------------------------------------------------------------------------
# a) Graceful degradation — missing myopic[semantic] extra
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """When CodeIndex.connect raises RuntimeError (missing extra), tools return {"error": ...}."""

    @staticmethod
    def _raise_no_extra(root: str):
        raise RuntimeError(
            "semantic search needs the optional extra — install with: pip install myopic[semantic]"
        )

    def _patch_all_connect(self, monkeypatch) -> None:
        """Patch CodeIndex.connect in every module namespace that holds a reference.

        indexer.py imports CodeIndex at module top level, so we patch both the
        class-level connect AND the module-level name in indexer_mod to be safe.
        code_search.py and review_context.py do lazy imports inside function
        bodies (from myopic.semantic.store import CodeIndex), so patching the
        class method is sufficient for those — but patching store_mod.CodeIndex.connect
        works for all since Python re-evaluates the import each call.
        """
        import myopic.semantic.store as store_mod

        monkeypatch.setattr(store_mod.CodeIndex, "connect", staticmethod(self._raise_no_extra))
        # Also patch the already-imported name in indexer_mod's namespace.
        monkeypatch.setattr(indexer_mod, "CodeIndex", type(
            "_NoExtra", (), {"connect": staticmethod(self._raise_no_extra)}
        ))

    def test_index_repo_returns_error_dict(self, monkeypatch, tmp_path):
        self._patch_all_connect(monkeypatch)

        # Also patch embed_texts so we don't need Ollama — the connect failure
        # fires first, but we stub embed_texts to be safe.
        monkeypatch.setattr(indexer_mod, "embed_texts", lambda texts: [[0.0] * 4 for _ in texts])

        # Write a .py file so the indexer actually reaches the CodeIndex call.
        (tmp_path / "hello.py").write_text("def hello(): pass\n", encoding="utf-8")

        result = index_repo_mod.index_repo(str(tmp_path))

        assert "error" in result
        assert "myopic[semantic]" in result["error"]

    def test_code_search_returns_error_dict(self, monkeypatch):
        self._patch_all_connect(monkeypatch)

        result = code_search_mod.code_search("find something", root="/fake/root")

        assert "error" in result
        assert "myopic[semantic]" in result["error"]

    def test_mr_review_context_semantic_unavailable(self, monkeypatch, tmp_path):
        """mr_review_context still returns a valid structure; semantic_available=False."""
        self._patch_all_connect(monkeypatch)

        patch = _make_patch("compute_fare")
        review = _FakeReview([FileDiff(file_path="fare.py", old_path="fare.py", patch=patch)])
        monkeypatch.setattr(review_context_mod, "open_review", lambda url: review)

        # dependency_impact needs a real path; point it at tmp_path (empty dir).
        result = review_context_mod.mr_review_context(
            url="http://gitlab/mr/99", root=str(tmp_path)
        )

        assert result["mr_number"] == 99
        assert result["semantic_available"] is False
        assert isinstance(result["symbols"], list)
        # Each symbol must have an impact key but NOT related_patterns.
        for sym_entry in result["symbols"]:
            assert "impact" in sym_entry
            assert "related_patterns" not in sym_entry


# ---------------------------------------------------------------------------
# b) Indexer chunk-collection and filtering
# ---------------------------------------------------------------------------


class _FakeCodeIndex:
    """Captures rows passed to replace() without touching real LanceDB."""

    def __init__(self):
        self.replaced_rows: list[dict] = []
        self.written_meta: dict | None = None

    def has_table(self) -> bool:
        return False

    def replace(self, rows: list[dict]) -> int:
        self.replaced_rows = list(rows)
        return len(rows)

    def read_meta(self) -> dict | None:
        return None

    def write_meta(self, meta: dict) -> None:
        self.written_meta = meta


class TestIndexerChunkCollection:
    """Verifies which files are walked/chunked and which are skipped."""

    def _build_repo(self, tmp_path):
        """Create a mini repo with a mix of file types."""
        # Supported: .py file at root
        (tmp_path / "main.py").write_text(
            "def hello():\n    return 1\n\ndef world():\n    return 2\n",
            encoding="utf-8",
        )
        # Supported: another .py file in a subdirectory
        subdir = tmp_path / "pkg"
        subdir.mkdir()
        (subdir / "utils.py").write_text(
            "class Helper:\n    pass\n",
            encoding="utf-8",
        )
        # SKIP_DIRS: node_modules — should be pruned entirely
        node_mod = tmp_path / "node_modules" / "lib"
        node_mod.mkdir(parents=True)
        (node_mod / "bundled.js").write_text("console.log('hi');", encoding="utf-8")

        # Unsupported extension — should be skipped
        (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")

        return tmp_path

    def test_only_supported_files_outside_skip_dirs_are_chunked(self, monkeypatch, tmp_path):
        repo = self._build_repo(tmp_path)

        fake_index = _FakeCodeIndex()

        def _fake_connect(root: str) -> _FakeCodeIndex:
            return fake_index

        monkeypatch.setattr(indexer_mod, "embed_texts", lambda texts: [[0.0] * 4 for _ in texts])

        # indexer.py does `from myopic.semantic.store import CodeIndex` at import
        # time, so we patch the name in the indexer module's namespace directly.
        _FakeCIClass = type("_FakeCIClass", (), {"connect": staticmethod(_fake_connect)})
        monkeypatch.setattr(indexer_mod, "CodeIndex", _FakeCIClass)

        from myopic.semantic.indexer import index_repo

        result = index_repo(str(repo))

        # Two .py files should have been processed.
        assert result["files"] == 2
        # README.md and node_modules content are skipped.
        assert result["skipped"] >= 1  # at least the .md file

        # All chunks must come from .py files only.
        for row in fake_index.replaced_rows:
            assert row["file_path"].endswith(".py"), (
                f"Unexpected file in index: {row['file_path']}"
            )
        # node_modules path must never appear.
        for row in fake_index.replaced_rows:
            assert "node_modules" not in row["file_path"]

        # Every row must have a vector field (from the fake embed).
        for row in fake_index.replaced_rows:
            assert "vector" in row
            assert row["vector"] == [0.0] * 4

        # Stats make sense.
        assert result["indexed_chunks"] == len(fake_index.replaced_rows)
        assert result["indexed_chunks"] > 0


# ---------------------------------------------------------------------------
# c) mr_review_context — graph-first fusion, semantic unavailable
# ---------------------------------------------------------------------------


class TestMrReviewContext:
    """mr_review_context returns the correct shape regardless of semantic availability."""

    def _make_fake_impact(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "root": "/fake",
            "total_references": 2,
            "references": [],
            "by_file": {},
            "by_usage_type": {},
        }

    def test_graph_first_without_semantic(self, monkeypatch, tmp_path):
        """All symbols get impact; no related_patterns; semantic_available=False."""
        patch = _make_patch("calculate_distance")
        review = _FakeReview(
            [FileDiff(file_path="geo.py", old_path="geo.py", patch=patch)],
            mr_number=7,
        )
        monkeypatch.setattr(review_context_mod, "open_review", lambda url: review)

        # Patch dependency_impact to return deterministic output.
        monkeypatch.setattr(
            review_context_mod,
            "dependency_impact",
            lambda symbol, root, **_kw: self._make_fake_impact(symbol),
        )

        # Make CodeIndex.connect raise so semantic path is skipped.
        import myopic.semantic.store as store_mod

        def _no_semantic(root: str):
            raise RuntimeError(
                "semantic search needs the optional extra — "
                "install with: pip install myopic[semantic]"
            )

        monkeypatch.setattr(store_mod.CodeIndex, "connect", staticmethod(_no_semantic))

        result = review_context_mod.mr_review_context(
            url="http://gitlab/mr/7",
            root=str(tmp_path),
            max_symbols=4,
        )

        assert result["mr_number"] == 7
        assert result["semantic_available"] is False
        assert isinstance(result["symbols"], list)
        assert len(result["symbols"]) <= 4

        for sym_entry in result["symbols"]:
            assert "symbol" in sym_entry
            assert "impact" in sym_entry
            # related_patterns must NOT appear when semantic is unavailable.
            assert "related_patterns" not in sym_entry

    def test_open_review_failure_returns_error(self, monkeypatch):
        def _bad_open(url: str):
            raise ValueError("bad URL")

        monkeypatch.setattr(review_context_mod, "open_review", _bad_open)
        result = review_context_mod.mr_review_context(
            url="http://not-gitlab/mr/1", root="/tmp"
        )
        assert "error" in result
        assert "Failed to open review" in result["error"]

    def test_max_symbols_cap_respected(self, monkeypatch, tmp_path):
        """At most max_symbols symbols are analyzed even if the diff has more."""
        # Craft a patch with many distinct identifiers.
        lines = "\n".join(
            f"+def symbol_{i}(x): return x + {i}" for i in range(20)
        )
        patch = f"@@ -0,0 +1,20 @@\n{lines}\n"
        review = _FakeReview(
            [FileDiff(file_path="many.py", old_path="many.py", patch=patch)]
        )
        monkeypatch.setattr(review_context_mod, "open_review", lambda url: review)
        monkeypatch.setattr(
            review_context_mod,
            "dependency_impact",
            lambda symbol, root, **_kw: self._make_fake_impact(symbol),
        )

        import myopic.semantic.store as store_mod

        def _no_semantic(root: str):
            raise RuntimeError(
                "semantic search needs the optional extra — "
                "install with: pip install myopic[semantic]"
            )

        monkeypatch.setattr(store_mod.CodeIndex, "connect", staticmethod(_no_semantic))

        result = review_context_mod.mr_review_context(
            url="http://gitlab/mr/5", root=str(tmp_path), max_symbols=3
        )

        assert len(result["symbols"]) <= 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
