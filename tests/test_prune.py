"""Prune classification: orphans, duplicate twins, and keeping the reachable one."""

import json

from myopic.semantic.prune import evict_twins, find_prunable, scan_indexes
from myopic.semantic.store import _table_name


def _meta(idx, name, *, root=None, remote=None, files=(), at="2026-01-01", chunks=10, lance=True):
    (idx / f"{name}.meta.json").write_text(json.dumps({
        "root": root, "remote": remote, "indexed_at": at, "chunks": chunks,
        "files": {f: "h" for f in files},
    }))
    if lance:
        d = idx / f"{name}.lance"
        d.mkdir(exist_ok=True)
        (d / "data").write_text("x" * 100)  # give it a nonzero size


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


def test_evict_drops_dead_twin_but_spares_a_live_second_clone(tmp_path):
    # The self-heal-on-index safety property: a second clone someone still uses
    # keeps its index; only unreachable twins are dropped.
    idx = tmp_path / "index"; idx.mkdir()
    cur, live = tmp_path / "cur", tmp_path / "clone_live"
    cur.mkdir(); live.mkdir()
    cur_key, live_key = _table_name(str(cur)), _table_name(str(live))

    _meta(idx, cur_key, root=str(cur), remote="host/x", files=["a", "b"], lance=False)
    _meta(idx, "deadtwin", root=str(tmp_path / "gone"), remote="host/x", files=["a", "b"], lance=False)
    _meta(idx, live_key, root=str(live), remote="host/x", files=["a", "b"], lance=False)

    dropped = evict_twins(cur_key, "host/x", frozenset({"a", "b"}), idx_dir=idx)

    assert dropped == ["deadtwin"]
    assert not (idx / "deadtwin.meta.json").exists()
    assert (idx / f"{live_key}.meta.json").exists()   # live clone left alone
    assert (idx / f"{cur_key}.meta.json").exists()    # never evicts itself


def test_evict_ignores_a_different_project(tmp_path):
    idx = tmp_path / "index"; idx.mkdir()
    cur = tmp_path / "cur"; cur.mkdir()
    cur_key = _table_name(str(cur))
    _meta(idx, cur_key, root=str(cur), remote="host/x", files=["a", "b"], lance=False)
    _meta(idx, "other", root=str(tmp_path / "gone"), remote="host/OTHER", files=["p", "q"], lance=False)
    assert evict_twins(cur_key, "host/x", frozenset({"a", "b"}), idx_dir=idx) == []
    assert (idx / "other.meta.json").exists()


def test_distinct_projects_not_pruned(tmp_path):
    idx = tmp_path / "index"; idx.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"; a.mkdir(); b.mkdir()
    _meta(idx, _table_name(str(a)), root=str(a), remote="host/a", files=["x", "y"])
    _meta(idx, _table_name(str(b)), root=str(b), remote="host/b", files=["p", "q"])
    rep = find_prunable([], idx_dir=idx)
    assert rep["prunable"] == [] and len(rep["keep"]) == 2
