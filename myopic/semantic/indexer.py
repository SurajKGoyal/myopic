"""
index_repo implementation — walk a repo, chunk by AST, embed, and store, with
incremental re-embedding and freshness tracking.

The first index of a repo is a full build. After that, index_repo re-embeds only
the files whose content changed since the last run (tracked by per-file content
hash in a JSON sidecar), so refreshing a large repo takes seconds, not minutes.
A change of embedding model, a missing sidecar, or force=True triggers a full
rebuild. The sidecar also records the repo's main-line sha the index approximates
(not the current checkout), which `index_status` uses to report "N commits behind".

Delegates lazy-import safety to embeddings.embed_texts and semantic.store.CodeIndex,
so this module has no top-level optional imports and can be imported by the base
myopic install without error.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from myopic import gitutil
from myopic.ast_chunker import ast_chunk
from myopic.config import embed_model
from myopic.diff import EXT_TO_LANG, SKIP_DIRS
from myopic.embeddings import embed_texts
from myopic.semantic.store import CodeIndex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chunk_file(rel_path: str, content: str, language: str) -> list[dict]:
    """Chunk one file's content into indexable rows (without vectors)."""
    ast_chunks = ast_chunk(content, language)
    if ast_chunks:
        return [
            {
                "file_path": rel_path,
                "symbol": symbol,
                "symbol_type": symbol_type,
                "start_line": start_line,
                "end_line": end_line,
                "text": chunk_text,
            }
            for chunk_text, start_line, end_line, symbol, symbol_type in ast_chunks
        ]
    # tree-sitter unavailable or parse failed — whole-file fallback.
    return [{
        "file_path": rel_path,
        "symbol": None,
        "symbol_type": "file",
        "start_line": 1,
        "end_line": content.count("\n") + 1,
        "text": content,
    }]


def _scan(root_path: Path) -> tuple[dict[str, list[dict]], dict[str, str], int]:
    """Walk the repo once. Return (chunks_by_file, content_hash_by_file, skipped)."""
    chunks_by_file: dict[str, list[dict]] = {}
    hash_by_file: dict[str, str] = {}
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            abs_file = Path(dirpath) / filename
            language = EXT_TO_LANG.get(abs_file.suffix.lower())
            if language is None:
                skipped += 1
                continue
            try:
                content = abs_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                skipped += 1
                continue
            if not content.strip():
                skipped += 1
                continue

            rel_path = str(abs_file.relative_to(root_path))
            hash_by_file[rel_path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunks_by_file[rel_path] = _chunk_file(rel_path, content, language)

    return chunks_by_file, hash_by_file, skipped


def _embed_rows(chunks: list[dict]) -> list[dict]:
    """Attach an embedding vector to each chunk row."""
    if not chunks:
        return []
    vectors = embed_texts([c["text"] for c in chunks])
    return [{**chunk, "vector": vector} for chunk, vector in zip(chunks, vectors)]


def index_repo(root: str, force: bool = False) -> dict:
    """Walk *root*, chunk by AST, embed, and store — incrementally when possible.

    Args:
        root:  Absolute path to the repository to index.
        force: Rebuild the whole index even if an up-to-date one exists.

    Returns:
        {mode: "full"|"incremental", indexed_chunks, files, skipped,
         changed_files, deleted_files, git_sha, model}

    Raises RuntimeError (from embed_texts / CodeIndex) if the semantic extra is
    not installed or Ollama is unreachable — the tool wrapper converts these to
    {"error": ...}.
    """
    root_path = Path(root).resolve()
    idx = CodeIndex.connect(str(root_path))
    model = embed_model()
    # Stamp the index with the repo's MAIN-line sha (the corpus it approximates),
    # not the current checkout — so indexing while on a feature branch / MR head
    # isn't read as perpetually "stale vs main". Falls back to HEAD for repos with
    # no resolvable default branch. (Freshness is judged against main in index_status.)
    git_sha = gitutil.default_branch_sha(str(root_path)) or gitutil.head_sha(str(root_path))

    chunks_by_file, hash_by_file, skipped = _scan(root_path)
    meta = idx.read_meta()

    # A full rebuild is required when there's no prior index, no usable metadata,
    # the embedding model changed (old vectors are meaningless), or force=True.
    incremental = (
        not force
        and idx.has_table()
        and meta is not None
        and meta.get("model") == model
        and isinstance(meta.get("files"), dict)
    )

    if not incremental:
        return _full_index(idx, chunks_by_file, hash_by_file, skipped, model, git_sha)

    prev_hashes: dict[str, str] = meta["files"]
    changed_files = [f for f, h in hash_by_file.items() if prev_hashes.get(f) != h]
    deleted_files = [f for f in prev_hashes if f not in hash_by_file]

    files_processed = len(hash_by_file)

    if not changed_files and not deleted_files:
        # Content is identical; only refresh the freshness stamp (e.g. HEAD moved
        # via commits that didn't touch indexed files).
        total = idx.row_count()
        _write_meta(idx, hash_by_file, model, git_sha, total)
        return {
            "mode": "incremental", "indexed_chunks": total,
            "files": files_processed, "skipped": skipped,
            "changed_files": 0, "deleted_files": 0,
            "git_sha": gitutil.short(git_sha), "model": model,
        }

    changed_chunks: list[dict] = []
    for f in changed_files:
        changed_chunks.extend(chunks_by_file[f])

    try:
        rows = _embed_rows(changed_chunks)
        total = idx.apply_delta(rows, remove_paths=changed_files + deleted_files)
    except Exception:
        # Delta failed (schema drift, corrupt table, ...) — fall back to a clean
        # full rebuild rather than leaving a half-updated index.
        return _full_index(idx, chunks_by_file, hash_by_file, skipped, model, git_sha)

    _write_meta(idx, hash_by_file, model, git_sha, total)
    return {
        "mode": "incremental", "indexed_chunks": total,
        "files": files_processed, "skipped": skipped,
        "changed_files": len(changed_files), "deleted_files": len(deleted_files),
        "git_sha": gitutil.short(git_sha), "model": model,
    }


def _full_index(idx, chunks_by_file, hash_by_file, skipped, model, git_sha) -> dict:
    all_chunks: list[dict] = []
    for chunks in chunks_by_file.values():
        all_chunks.extend(chunks)

    if not all_chunks:
        return {
            "mode": "full", "indexed_chunks": 0, "files": len(hash_by_file),
            "skipped": skipped, "changed_files": 0, "deleted_files": 0,
            "git_sha": gitutil.short(git_sha), "model": model,
        }

    rows = _embed_rows(all_chunks)
    total = idx.replace(rows)
    _write_meta(idx, hash_by_file, model, git_sha, total)
    return {
        "mode": "full", "indexed_chunks": total, "files": len(hash_by_file),
        "skipped": skipped, "changed_files": len(hash_by_file), "deleted_files": 0,
        "git_sha": gitutil.short(git_sha), "model": model,
    }


def _write_meta(idx, hash_by_file, model, git_sha, total) -> None:
    idx.write_meta({
        "git_sha": git_sha,
        "model": model,
        "chunks": total,
        "indexed_at": _now(),
        "files": hash_by_file,
    })


def _freshness(meta: dict, model: str, cur_sha: str | None, dirty: bool) -> tuple[str, str | None, bool]:
    """Pure freshness decision. Returns (state, reason, needs_commits_behind).

    Kept separate from I/O so it can be unit-tested without LanceDB or git.
    """
    if meta.get("model") != model:
        return (
            "model_mismatch",
            f"index built with {meta.get('model')!r}, current model is {model!r} — "
            "re-index (a full rebuild) so vectors match",
            False,
        )
    indexed_sha = meta.get("git_sha")
    if not cur_sha or not indexed_sha:
        return "unknown", None, False
    if cur_sha == indexed_sha:
        if dirty:
            return "stale", "uncommitted working-tree changes since indexing", False
        return "fresh", None, False
    return "stale", None, True


def index_status(root: str) -> dict:
    """Report whether a repo's semantic index is fresh, stale, or absent.

    Freshness is measured against the repo's MAIN line (origin's default branch,
    else local main/master), not the current checkout: if main has moved past the
    indexed commit, the index is "stale" and reports how many commits behind.
    Reviewing a feature-branch worktree does NOT mark it stale. A changed
    embedding model is "model_mismatch"; an older index without metadata is
    "unknown".

    Returns state = absent | fresh | stale | model_mismatch | unknown, plus
    context (compared_against, checkout_sha, indexed_sha, commits_behind). Raises
    RuntimeError if the semantic extra is missing — the wrapper converts it.
    """
    root_path = str(Path(root).resolve())
    idx = CodeIndex.connect(root_path)

    if not idx.has_table():
        return {"state": "absent", "root": root_path}

    model = embed_model()
    # Freshness is measured against the repo's MAIN line, not the current
    # checkout — so reviewing a feature-branch worktree doesn't make the index
    # look stale. Only when main has actually moved past the indexed commit do we
    # report "stale". Falls back to the checkout HEAD for repos without a
    # resolvable default branch (or non-git dirs).
    default_ref = gitutil.default_branch_ref(root_path)
    main_sha = gitutil.sha_of(root_path, default_ref) if default_ref else None
    compare_sha = main_sha or gitutil.head_sha(root_path)
    # A worktree's uncommitted changes don't reflect main; only weigh dirtiness
    # in the fallback (checkout-based) mode.
    dirty = gitutil.is_dirty(root_path)
    dirty_for_decision = dirty if main_sha is None else False

    out: dict = {
        "root": root_path,
        "chunks": idx.row_count(),
        "current_model": model,
        "compared_against": gitutil.short(compare_sha),
        "checkout_sha": gitutil.short(gitutil.head_sha(root_path)),
        "dirty": dirty,
    }

    meta = idx.read_meta()
    if not meta:
        out["state"] = "unknown"
        out["reason"] = (
            "indexed by an older myopic without freshness metadata — "
            "re-index to enable staleness tracking"
        )
        return out

    out["indexed_at"] = meta.get("indexed_at")
    out["indexed_sha"] = gitutil.short(meta.get("git_sha"))
    out["indexed_model"] = meta.get("model")

    state, reason, needs_behind = _freshness(meta, model, compare_sha, dirty_for_decision)
    out["state"] = state
    if reason:
        out["reason"] = reason
    if needs_behind:
        out["commits_behind"] = gitutil.commits_behind(
            root_path, meta.get("git_sha"), default_ref or "HEAD"
        )
    return out
