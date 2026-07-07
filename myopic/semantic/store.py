"""
CodeIndex — thin LanceDB wrapper for hybrid (vector + FTS) code search.

Lazily imports lancedb so the base myopic install never requires it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from myopic.config import index_dir


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
                "semantic search needs the optional extra — install with: pip install myopic[semantic]"
            ) from exc

        root_path = Path(root).resolve()
        idx_dir = index_dir()
        idx_dir.mkdir(parents=True, exist_ok=True)
        # Stable per-repo table name: first 16 hex chars of SHA-256 of the resolved path.
        table_name = hashlib.sha256(str(root_path).encode("utf-8")).hexdigest()[:16]
        db = lancedb.connect(str(idx_dir))
        return cls(root_path, db, table_name)

    def has_table(self) -> bool:
        """Return True if this repo has already been indexed."""
        return self._table_name in self._db.table_names()

    def replace(self, rows: list[dict]) -> int:
        """Overwrite the index with new rows. Each row needs 'vector', 'text', and metadata.

        Returns the number of rows written.
        """
        table = self._db.create_table(self._table_name, data=rows, mode="overwrite")
        table.create_fts_index("text", replace=True)
        return len(rows)

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
