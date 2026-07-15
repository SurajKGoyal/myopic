"""
Prune stale semantic indexes.

Each indexed repo is one LanceDB table keyed by its git-common-dir, so a clone and
all its linked worktrees share one table (index once, reuse across branches). But
two *separate clones* of the same repo — or a repo you move/delete — leave tables
that searches never hit again and nothing ever refreshes. This finds them:

  - **orphan**   — the checkout the index was built from is gone from disk.
  - **duplicate**— several tables are the same project (same origin remote, or a
                   near-identical file set); keep the one that's still reachable
                   (its key matches a repo on disk), drop the rest.

Dry-run by design: `find_prunable` reports, `prune(apply=True)` deletes.
"""

from __future__ import annotations

import json
from pathlib import Path

from myopic.config import index_dir
from myopic.semantic.store import _table_name

_META_SUFFIX = ".meta.json"
_DUP_JACCARD = 0.6  # file-set overlap above which two tables are "the same project"


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scan_indexes(idx_dir: Path | None = None) -> list[dict]:
    """One record per index table found in the index dir (from its meta sidecar)."""
    d = idx_dir or index_dir()
    records = []
    for meta_file in sorted(d.glob(f"*{_META_SUFFIX}")):
        name = meta_file.name[: -len(_META_SUFFIX)]
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
        files = meta.get("files") or {}
        records.append({
            "name": name,
            "root": meta.get("root"),
            "remote": meta.get("remote"),
            "chunks": meta.get("chunks"),
            "indexed_at": meta.get("indexed_at") or "",
            "fileset": frozenset(files.keys() if isinstance(files, dict) else files),
            "size_bytes": _dir_size(d / f"{name}.lance"),
        })
    return records


def live_keys(scan_dirs: list[str], records: list[dict]) -> set[str]:
    """Table keys that are still reachable — i.e. equal the current git-common-dir
    key of a repo that exists on disk. Discovered from `scan_dirs` (walked for git
    repos) plus every existing `root` recorded in a meta."""
    keys: set[str] = set()
    roots: set[Path] = set()
    for r in records:
        if r["root"] and Path(r["root"]).exists():
            roots.add(Path(r["root"]))
    for base in scan_dirs:
        b = Path(base).expanduser()
        if not b.exists():
            continue
        for gitdir in b.rglob(".git"):
            if gitdir.is_dir():
                roots.add(gitdir.parent)
    for root in roots:
        try:
            keys.add(_table_name(str(root.resolve())))
        except Exception:  # noqa: BLE001 — best-effort reachability
            continue
    return keys


def _group_duplicates(records: list[dict]) -> list[list[dict]]:
    """Greedy grouping: same non-empty origin remote, or file-set overlap ≥ threshold."""
    groups: list[list[dict]] = []
    for r in records:
        for g in groups:
            rep = g[0]
            same_remote = r["remote"] and rep["remote"] and r["remote"] == rep["remote"]
            if same_remote or _jaccard(r["fileset"], rep["fileset"]) >= _DUP_JACCARD:
                g.append(r)
                break
        else:
            groups.append([r])
    return groups


def _keeper(group: list[dict], reachable: set[str]) -> dict:
    """The table to KEEP in a duplicate group: prefer the reachable one (searches
    hit it), then an existing on-disk root, then the most recently indexed."""
    def rank(r: dict) -> tuple:
        return (
            r["name"] in reachable,
            bool(r["root"]) and Path(r["root"]).exists(),
            r["indexed_at"],
            r["chunks"] or 0,
        )
    return max(group, key=rank)


def find_prunable(scan_dirs: list[str] | None = None, idx_dir: Path | None = None) -> dict:
    """Classify every index table. Returns {prunable: [...], keep: [...], reachable}.

    A record is prunable when it's an orphan (its `root` is gone and it isn't
    reachable) or a duplicate (a same-project sibling is the keeper).
    """
    records = scan_indexes(idx_dir)
    reachable = live_keys(scan_dirs or [], records)
    prunable: list[dict] = []
    survivors: list[dict] = []

    for r in records:
        root, name = r["root"], r["name"]
        if root and not Path(root).exists() and name not in reachable:
            prunable.append({**r, "reason": "orphan — checkout gone"})
        else:
            survivors.append(r)

    keep: list[dict] = []
    for group in _group_duplicates(survivors):
        if len(group) == 1:
            keep.append(group[0])
            continue
        winner = _keeper(group, reachable)
        for r in group:
            if r["name"] == winner["name"]:
                keep.append(r)
            else:
                prunable.append({**r, "reason": f"duplicate of {winner['name'][:8]}"})

    return {"prunable": prunable, "keep": keep, "reachable": reachable}


def _is_reachable(record: dict) -> bool:
    """True when this table is still the live index for an on-disk checkout — i.e.
    its name equals that checkout's current git-common-dir key."""
    root = record.get("root")
    if not root or not Path(root).exists():
        return False
    try:
        return _table_name(root) == record["name"]
    except Exception:  # noqa: BLE001 — best-effort
        return False


def _drop(idx_dir: Path, name: str) -> None:
    import lancedb

    db = lancedb.connect(str(idx_dir))
    if name in set(db.list_tables().tables):
        db.drop_table(name)
    (idx_dir / f"{name}{_META_SUFFIX}").unlink(missing_ok=True)


def evict_twins(name: str, remote: str | None, fileset: frozenset,
                *, idx_dir: Path | None = None) -> list[str]:
    """Drop indexes that are the same project as the just-indexed table `name` but
    are no longer reachable (checkout gone, or the key no longer matches it).

    Called after a successful index so duplicates self-heal instead of piling up. A
    second clone that is still LIVE is deliberately left alone — someone may be
    using it; only dead twins go. Returns the names dropped.
    """
    d = idx_dir or index_dir()
    dropped: list[str] = []
    for r in scan_indexes(d):
        if r["name"] == name:
            continue
        same_project = (
            (bool(remote) and r["remote"] == remote)
            or _jaccard(r["fileset"], fileset) >= _DUP_JACCARD
        )
        if same_project and not _is_reachable(r):
            _drop(d, r["name"])
            dropped.append(r["name"])
    return dropped


def prune(scan_dirs: list[str] | None = None, *, apply: bool = False,
          idx_dir: Path | None = None) -> dict:
    """Find and (when apply=True) delete prunable index tables. Returns a report
    including reclaimed bytes."""
    report = find_prunable(scan_dirs, idx_dir)
    reclaimed = sum(r["size_bytes"] for r in report["prunable"])
    if apply and report["prunable"]:
        d = idx_dir or index_dir()
        for r in report["prunable"]:
            _drop(d, r["name"])
    report["reclaimed_bytes"] = reclaimed
    report["applied"] = apply
    return report
