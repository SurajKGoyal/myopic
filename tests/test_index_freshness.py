"""
Tests for index freshness: the pure state decision (hermetic) and the
incremental re-index + index_status flow (gated on LanceDB being installed).
"""

from __future__ import annotations

import importlib.util

import pytest

from myopic.semantic.indexer import _freshness

_LANCEDB = importlib.util.find_spec("lancedb") is not None


# --- pure freshness decision (no LanceDB, no git) ---------------------------

class TestFreshnessDecision:
    def test_fresh(self):
        state, reason, needs = _freshness({"model": "m", "git_sha": "abc"}, "m", "abc", False)
        assert state == "fresh" and reason is None and needs is False

    def test_dirty_same_sha_is_stale(self):
        state, reason, _ = _freshness({"model": "m", "git_sha": "abc"}, "m", "abc", True)
        assert state == "stale" and "uncommitted" in reason

    def test_moved_head_is_stale_needs_behind(self):
        state, _, needs = _freshness({"model": "m", "git_sha": "old"}, "m", "new", False)
        assert state == "stale" and needs is True

    def test_model_mismatch(self):
        state, reason, _ = _freshness({"model": "old", "git_sha": "abc"}, "new", "abc", False)
        assert state == "model_mismatch" and "re-index" in reason

    def test_unknown_without_sha(self):
        assert _freshness({"model": "m", "git_sha": None}, "m", None, False)[0] == "unknown"


# --- incremental indexing + status (needs LanceDB) --------------------------

@pytest.mark.skipif(not _LANCEDB, reason="incremental index path needs lancedb")
class TestIncrementalIndex:
    def _prep(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MYOPIC_HOME", str(tmp_path / "home"))
        import myopic.semantic.indexer as ix
        # Deterministic 4-d embeddings; no Ollama.
        monkeypatch.setattr(
            ix, "embed_texts",
            lambda texts: [[float(len(t) % 5), 1.0, 0.0, 0.0] for t in texts],
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (repo / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        return ix, repo

    def test_full_then_incremental(self, monkeypatch, tmp_path):
        ix, repo = self._prep(monkeypatch, tmp_path)
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha1")

        r1 = ix.index_repo(str(repo))
        assert r1["mode"] == "full" and r1["files"] == 2 and r1["indexed_chunks"] >= 2

        # Change only a.py, move HEAD.
        (repo / "a.py").write_text("def a():\n    return 100\n", encoding="utf-8")
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha2")
        r2 = ix.index_repo(str(repo))
        assert r2["mode"] == "incremental"
        assert r2["changed_files"] == 1 and r2["deleted_files"] == 0

    def test_deletion_is_pruned(self, monkeypatch, tmp_path):
        ix, repo = self._prep(monkeypatch, tmp_path)
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha1")
        ix.index_repo(str(repo))
        (repo / "b.py").unlink()
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha2")
        r = ix.index_repo(str(repo))
        assert r["deleted_files"] == 1

    def test_status_fresh_then_stale(self, monkeypatch, tmp_path):
        ix, repo = self._prep(monkeypatch, tmp_path)
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha1")
        monkeypatch.setattr(ix.gitutil, "is_dirty", lambda root: False)
        ix.index_repo(str(repo))

        st = ix.index_status(str(repo))
        assert st["state"] == "fresh"

        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha2")
        monkeypatch.setattr(ix.gitutil, "commits_behind", lambda root, old, ref="HEAD": 3)
        st2 = ix.index_status(str(repo))
        assert st2["state"] == "stale" and st2["commits_behind"] == 3

    def test_status_absent(self, monkeypatch, tmp_path):
        ix, repo = self._prep(monkeypatch, tmp_path)
        st = ix.index_status(str(repo))
        assert st["state"] == "absent"

    def test_force_rebuilds(self, monkeypatch, tmp_path):
        ix, repo = self._prep(monkeypatch, tmp_path)
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "sha1")
        ix.index_repo(str(repo))
        r = ix.index_repo(str(repo), force=True)
        assert r["mode"] == "full"

    def test_freshness_tracks_main_not_checkout(self, monkeypatch, tmp_path):
        """A feature-branch checkout stays 'fresh'; only main moving marks stale."""
        ix, repo = self._prep(monkeypatch, tmp_path)
        # Index while main = M1 (git_sha stored from head_sha).
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "M1")
        monkeypatch.setattr(ix.gitutil, "default_branch_ref", lambda root: "main")
        monkeypatch.setattr(ix.gitutil, "sha_of", lambda root, ref: "M1")
        ix.index_repo(str(repo))

        # Now HEAD is a feature branch (FEAT), but main is still M1.
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "FEAT")
        st = ix.index_status(str(repo))
        assert st["state"] == "fresh"        # checkout differs, but main matches

        # main advances to M2 → stale, with commits-behind against main.
        monkeypatch.setattr(ix.gitutil, "sha_of", lambda root, ref: "M2")
        monkeypatch.setattr(ix.gitutil, "commits_behind", lambda root, old, ref="HEAD": 2)
        st2 = ix.index_status(str(repo))
        assert st2["state"] == "stale" and st2["commits_behind"] == 2

    def test_indexing_off_main_stamps_main_not_checkout(self, monkeypatch, tmp_path):
        """Indexing while checked out on a feature branch / MR head stamps the MAIN
        sha, so a freshly-built index isn't read as perpetually 'stale vs main'."""
        ix, repo = self._prep(monkeypatch, tmp_path)
        monkeypatch.setattr(ix.gitutil, "head_sha", lambda root: "FEAT")     # on a feature branch
        monkeypatch.setattr(ix.gitutil, "default_branch_ref", lambda root: "main")
        monkeypatch.setattr(ix.gitutil, "sha_of", lambda root, ref: "M1")    # main is M1
        monkeypatch.setattr(ix.gitutil, "is_dirty", lambda root: False)

        r = ix.index_repo(str(repo))
        assert r["git_sha"] == "M1"          # stamped MAIN, not the FEAT checkout

        st = ix.index_status(str(repo))
        assert st["state"] == "fresh"        # not stale, despite the off-main checkout
