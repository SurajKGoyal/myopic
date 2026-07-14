"""
CodeIndex — thin LanceDB wrapper for hybrid (vector + FTS) code search.

Lazily imports lancedb so the base myopic install never requires it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from myopic import gitutil
from myopic.config import index_dir


def _repo_key(root: str) -> str:
    """Identity used to key a repo's index.

    The git repository's shared common dir — so the main clone and all of its
    worktrees resolve to the SAME index (index once, reuse across every branch/MR;
    switching to a source branch only re-embeds the changed files). Falls back to
    the resolved path when `root` isn't a git repo.
    """
    return gitutil.common_dir(root) or str(Path(root).resolve())


def _table_name(root: str) -> str:
    return hashlib.sha256(_repo_key(root).encode("utf-8")).hexdigest()[:16]


def _create_fts_index(table) -> None:
    """Build (or replace) the full-text index on the 'text' column.

    Uses the current LanceDB API — create_index with an FTS config; the old
    create_fts_index was deprecated in 0.25. FTS is imported lazily so the base
    install (which never imports lancedb) stays import-safe.
    """
    from lancedb.index import FTS

    table.create_index("text", config=FTS(), replace=True)


class CodeIndex:
    """A per-repo LanceDB table for hybrid code search."""

    def __init__(self, root: Path, db, table_name: str) -> None:
        self._root = root
        self._db = db
        self._table_name = table_name

    @classmethod
    def connect(cls, root: str) -> "CodeIndex":
        """Open (creating the directory if needed) the LanceDB index for a repo.

        Raises RuntimeError with the install-the-extra message if lancedb is missing.
        """
        try:
            import lancedb
        except ImportError as exc:
            raise RuntimeError(
                "the semantic layer is bundled — reinstall myopic if this import fails"
            ) from exc

        root_path = Path(root).resolve()
        idx_dir = index_dir()
        idx_dir.mkdir(parents=True, exist_ok=True)
        # Keyed by the git repository (shared by all worktrees), not the checkout
        # path — so a worktree reuses its clone's index instead of rebuilding one.
        table_name = _table_name(str(root_path))
        db = lancedb.connect(str(idx_dir))
        return cls(root_path, db, table_name)

    @property
    def root(self) -> Path:
        """The checkout path this index was opened for."""
        return self._root

    @property
    def table_name(self) -> str:
        return self._table_name

    def drop_table(self) -> bool:
        """Delete this repo's LanceDB table and its freshness sidecar. Returns True
        if a table was dropped. Used by `myopic prune` to reclaim stale indexes."""
        existed = self.has_table()
        if existed:
            self._db.drop_table(self._table_name)
        self.delete_meta()
        return existed

    def has_table(self) -> bool:
        """Return True if this repo has already been indexed."""
        # list_tables() returns a ListTablesResponse; .tables is the name list
        # (no pagination when limit is unset — the default).
        return self._table_name in self._db.list_tables().tables

    def row_count(self) -> int:
        """Number of chunks currently stored for this repo (0 if not indexed)."""
        if not self.has_table():
            return 0
        return self._db.open_table(self._table_name).count_rows()

    def replace(self, rows: list[dict]) -> int:
        """Overwrite the index with new rows. Each row needs 'vector', 'text', and metadata.

        Returns the number of rows written.
        """
        table = self._db.create_table(self._table_name, data=rows, mode="overwrite")
        _create_fts_index(table)
        return len(rows)

    def apply_delta(self, changed_rows: list[dict], remove_paths: list[str]) -> int:
        """Incrementally update the index: drop rows for the given file paths, then
        add the new rows. Used for re-indexing only the files that changed.

        `remove_paths` should include every file whose chunks must go — both
        changed files (their old chunks) and deleted files. `changed_rows` are the
        freshly-embedded chunks for the changed files. Returns the new total count.
        """
        table = self._db.open_table(self._table_name)

        # Delete in bounded batches so a huge changeset can't build an
        # unmanageable predicate string.
        unique_paths = sorted(set(remove_paths))
        for start in range(0, len(unique_paths), 400):
            batch = unique_paths[start:start + 400]
            quoted = ",".join("'" + p.replace("'", "''") + "'" for p in batch)
            table.delete(f"file_path IN ({quoted})")

        if changed_rows:
            table.add(changed_rows)

        # FTS index must be rebuilt after mutating the table.
        _create_fts_index(table)
        return table.count_rows()

    # --- freshness metadata (JSON sidecar next to the LanceDB table) ---------

    def _meta_path(self) -> Path:
        return index_dir() / f"{self._table_name}.meta.json"

    def read_meta(self) -> dict | None:
        """Read the index freshness sidecar, or None if it doesn't exist / is unreadable."""
        path = self._meta_path()
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def write_meta(self, meta: dict) -> None:
        """Write the index freshness sidecar (git sha, model, file hashes, ...)."""
        self._meta_path().write_text(json.dumps(meta), encoding="utf-8")

    def delete_meta(self) -> None:
        """Remove the freshness sidecar (used when clearing an index)."""
        self._meta_path().unlink(missing_ok=True)

    def hybrid_search(
        self, query_text: str, query_vector: list[float], k: int = 8
    ) -> list[dict]:
        """Hybrid vector+FTS search with RRF reranking. Returns list of row dicts."""
        from lancedb.rerankers import RRFReranker

        table = self._db.open_table(self._table_name)
        results = (
            table.search(query_type="hybrid")
            .vector(query_vector)
            .text(query_text)
            .rerank(RRFReranker())
            .limit(k)
            .to_list()
        )
        return results
