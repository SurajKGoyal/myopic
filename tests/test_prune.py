"""Prune classification: orphans, duplicate twins, and keeping the reachable one."""

import json

from myopic.semantic.prune import find_prunable, scan_indexes
from myopic.semantic.store import _table_name


def _meta(idx, name, *, root=None, remote=None, files=(), at="2026-01-01", chunks=10):
    (idx / f"{name}.meta.json").write_text(json.dumps({
        "root": root, "remote": remote, "indexed_at": at, "chunks": chunks,
        "files": {f: "h" for f in files},
    }))
    lance = idx / f"{name}.lance"
    lance.mkdir(exist_ok=True)
    (lance / "data").write_text("x" * 100)  # give it a nonzero size


def test_scan_reads_size_and_fields(tmp_path):
    idx = tmp_path / "index"; idx.mkdir()
    _meta(idx, "aaaa", root=str(tmp_path), remote="host/x", files=["a.py"])
    rec = scan_indexes(idx)[0]
    assert rec["name"] == "aaaa" and rec["remote"] == "host/x" and rec["size_bytes"] > 0


def test_orphan_detected(tmp_path):
    idx = tmp_path / "index"; idx.mkdir()
    _meta(idx, "aaaa", root=str(tmp_path / "gone"), files=["a.py"])
    rep = find_prunable([], idx_dir=idx)
    assert [r["name"] for r in rep["prunable"]] == ["aaaa"]
    assert "orphan" in rep["prunable"][0]["reason"]


def test_duplicate_keeps_reachable_over_newer(tmp_path):
    # The hdfc case: the NEWER table is the stale twin; the reachable one must win.
    idx = tmp_path / "index"; idx.mkdir()
    live, twin = tmp_path / "clone_a", tmp_path / "clone_b"
    live.mkdir(); twin.mkdir()
    live_key = _table_name(str(live))  # this table's name == its own key → reachable
    _meta(idx, live_key, root=str(live), remote="host/x", files=["a", "b", "c"], at="2026-07-08")
    _meta(idx, "twinkey", root=str(twin), remote="host/x", files=["a", "b", "c"], at="2026-07-14")
    rep = find_prunable([], idx_dir=idx)
    assert [r["name"] for r in rep["prunable"]] == ["twinkey"]
    assert any(r["name"] == live_key for r in rep["keep"])


def test_duplicate_by_fileset_without_remote(tmp_path):
    # Legacy tables (no remote): grouped by file-set overlap.
    idx = tmp_path / "index"; idx.mkdir()
    live = tmp_path / "repo"; live.mkdir()
    live_key = _table_name(str(live))
    _meta(idx, live_key, root=str(live), files=["a", "b", "c", "d"], at="2026-07-11")
    _meta(idx, "legacytwin", files=["a", "b", "c", "e"], at="2026-07-08")  # no root, jaccard 0.6
    rep = find_prunable([], idx_dir=idx)
    assert [r["name"] for r in rep["prunable"]] == ["legacytwin"]


def test_distinct_projects_not_pruned(tmp_path):
    idx = tmp_path / "index"; idx.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"; a.mkdir(); b.mkdir()
    _meta(idx, _table_name(str(a)), root=str(a), remote="host/a", files=["x", "y"])
    _meta(idx, _table_name(str(b)), root=str(b), remote="host/b", files=["p", "q"])
    rep = find_prunable([], idx_dir=idx)
    assert rep["prunable"] == [] and len(rep["keep"]) == 2
