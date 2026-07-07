"""
index_repo implementation — walk a repo, chunk by AST, embed, and store.

Delegates lazy-import safety to embeddings.embed_texts and semantic.store.CodeIndex,
so this module itself has no top-level optional imports and can be imported by the
base myopic install without error. RuntimeErrors from the helpers propagate up to
the tool wrapper (myopic/tools/index_repo.py) for conversion to {"error": ...}.
"""

from __future__ import annotations

import os
from pathlib import Path

from myopic.ast_chunker import ast_chunk
from myopic.diff import EXT_TO_LANG, SKIP_DIRS
from myopic.embeddings import embed_texts
from myopic.semantic.store import CodeIndex


def index_repo(root: str) -> dict:
    """Walk *root*, chunk every supported-language file by AST, embed, and store.

    Returns:
        {"indexed_chunks": int, "files": int, "skipped": int}

    Raises RuntimeError (from embed_texts / CodeIndex) if the semantic extra
    is not installed or Ollama is unreachable — the tool wrapper converts these
    to {"error": ...}.
    """
    root_path = Path(root).resolve()
    chunks: list[dict] = []
    files_processed = 0
    files_skipped = 0

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune SKIP_DIRS in-place so os.walk won't descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            abs_file = Path(dirpath) / filename
            suffix = abs_file.suffix.lower()
            language = EXT_TO_LANG.get(suffix)

            if language is None:
                files_skipped += 1
                continue

            try:
                content = abs_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                files_skipped += 1
                continue

            if not content.strip():
                files_skipped += 1
                continue

            rel_path = str(abs_file.relative_to(root_path))
            ast_chunks = ast_chunk(content, language)

            if ast_chunks:
                for chunk_text, start_line, end_line, symbol, symbol_type in ast_chunks:
                    chunks.append({
                        "file_path": rel_path,
                        "symbol": symbol,
                        "symbol_type": symbol_type,
                        "start_line": start_line,
                        "end_line": end_line,
                        "text": chunk_text,
                    })
            else:
                # tree-sitter unavailable or parse failed — whole-file fallback.
                line_count = content.count("\n") + 1
                chunks.append({
                    "file_path": rel_path,
                    "symbol": None,
                    "symbol_type": "file",
                    "start_line": 1,
                    "end_line": line_count,
                    "text": content,
                })

            files_processed += 1

    if not chunks:
        return {"indexed_chunks": 0, "files": files_processed, "skipped": files_skipped}

    # Embed all chunks in one call (embed_texts batches internally).
    vectors = embed_texts([c["text"] for c in chunks])
    rows = [{**chunk, "vector": vector} for chunk, vector in zip(chunks, vectors)]

    indexed = CodeIndex.connect(root).replace(rows)
    return {
        "indexed_chunks": indexed,
        "files": files_processed,
        "skipped": files_skipped,
    }
