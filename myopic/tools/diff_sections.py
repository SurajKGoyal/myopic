"""
mr_diff_sections — AST-grouped diff, token-budgeted for large reviews.

Groups changed lines by their enclosing function/class symbol instead of
returning raw line-by-line hunks. Dramatically reduces output size for large
reviews while preserving 100% of the changed lines.

Strategy:
  - New files in a supported language: full AST chunking via ast_chunker.
  - Modified files: hunk header context hint (@@ ... @@ funcName) + nearest
    preceding declaration pattern in context lines.
  - All changed lines (add/del) are always included — nothing dropped.
  - Context lines included only when include_context_lines=True.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from myopic.ast_chunker import ast_chunk
from myopic.diff import EXT_TO_LANG, classify_file, count_lines, parse_hunks
from myopic.platforms.base import open_review
from myopic.symbol_patterns import DECL_PATTERNS

# Matches the function context hint appended after @@ -x,y +x,y @@
_HUNK_HDR_RE = re.compile(r"^@@ [^@]+ @@ ?(.+)?$")

# Extracts the first identifier after a keyword in a hint string
_KEYWORD_NAME_RE = re.compile(
    r"\b(?:fun|def|func|function|class|interface|object|struct|impl|trait|enum)\s+(\w+)"
)
_MODIFIER_NAME_RE = re.compile(
    r"\b(?:override|private|public|protected|internal|suspend|async|static|"
    r"abstract|sealed|data|open|inline)\s+(?:\w+\s+)*?"
    r"(?:fun|def|func|function|class|interface|struct|enum|trait)\s+(\w+)"
)


def mr_diff_sections(
    url: str,
    include_context_lines: bool = False,
    files_filter: list[str] | None = None,
    max_chars: int = 80000,
    skip_noise: bool = True,
) -> dict:
    """
    Fetch a merge/pull request diff and group changes by enclosing function/class.

    All changed lines (add/del) are always present — no information dropped.
    Context lines (unchanged surrounding code) are optional. Files can be
    filtered to fetch only a subset for targeted review.

    Args:
        url: Full review URL (GitLab merge request).
        include_context_lines: Include unchanged surrounding lines in each
            section. Default False keeps the payload small.
        files_filter: Optional list of file-path fragments to include. Passing
            this is a TARGETED fetch — noise-skip and the budget are disabled
            so you get exactly the files you ask for.
        max_chars: Token-safety budget for the returned diff body. Ignored for
            targeted fetches.
        skip_noise: Keep lockfiles/generated/binary out of the body (listed
            under skipped_files). Default True. Ignored when filtering.

    Returns:
        {mr_number, title, author, source_branch, target_branch, description,
         commits, diff_shas, files[{file_path, language, new_file,
         deleted_file, additions, deletions,
         sections[{symbol, symbol_type, start_line, end_line, changes[...]}]}],
         truncated, omitted_files, skipped_files, stats}

    Budget: when the assembled diff would exceed max_chars, remaining files are
    listed in omitted_files (with stats) and truncated=True instead of
    returning an oversized payload. Passing files_filter is a targeted fetch —
    noise-skip and budget are both disabled so you always get exactly what you
    asked for.
    """
    try:
        review = open_review(url)
    except Exception as exc:
        return {"error": f"Failed to open review: {exc}"}

    try:
        meta = review.metadata()
        diff_set = review.diffs()
    except Exception as exc:
        return {"error": f"Failed to fetch diff: {exc}"}

    files: list[dict] = []
    omitted_files: list[dict] = []   # reviewable but cut for the budget — refetch via files_filter
    skipped_files: list[dict] = []   # noise — kept out of the body
    total_adds = 0
    total_dels = 0
    used_chars = 0
    budget_hit = False

    # An explicit files_filter is an intentional targeted fetch — honor it fully:
    # don't skip "noise" and don't budget-truncate, or the caller can't retrieve
    # what they asked for.
    targeted = bool(files_filter)

    for fd in diff_set.files:
        file_path = fd.file_path

        if files_filter and not any(
            f in file_path or file_path.endswith(f) for f in files_filter
        ):
            continue

        additions, deletions = count_lines(fd.patch)
        total_adds += additions
        total_dels += deletions

        # Noise files (lockfiles, generated, binary, huge) stay out of the diff
        # body by default — listed, not expanded — unless targeted explicitly.
        reviewable, skip_reason = classify_file(file_path, additions, deletions)
        if skip_noise and not reviewable and not targeted:
            skipped_files.append({
                "file_path": file_path, "additions": additions,
                "deletions": deletions, "reason": skip_reason,
            })
            continue

        # Once the budget is spent, record remaining files without building them.
        if budget_hit:
            omitted_files.append({
                "file_path": file_path, "additions": additions,
                "deletions": deletions, "reason": "token budget reached",
            })
            continue

        ext = Path(file_path).suffix.lower()
        language = EXT_TO_LANG.get(ext)

        hunks = parse_hunks(fd.patch)
        sections = _build_sections(
            fd.patch, hunks, language, fd.new_file, include_context_lines
        )

        file_entry = {
            "file_path": file_path,
            "language": language,
            "new_file": fd.new_file,
            "deleted_file": fd.deleted_file,
            "additions": additions,
            "deletions": deletions,
            "sections": sections,
        }

        # Budget check (skipped when targeted). Always emit at least one file so a
        # single over-budget file still returns *something* rather than nothing.
        if not targeted:
            entry_chars = len(json.dumps(file_entry))
            if files and used_chars + entry_chars > max_chars:
                budget_hit = True
                omitted_files.append({
                    "file_path": file_path, "additions": additions,
                    "deletions": deletions, "reason": "token budget reached",
                })
                continue
            used_chars += entry_chars

        files.append(file_entry)

    result = {
        "mr_number": meta.number,
        "title": meta.title,
        "author": meta.author,
        "source_branch": meta.source_branch,
        "target_branch": meta.target_branch,
        "description": meta.description,
        "commits": meta.commits,
        "diff_shas": diff_set.shas,
        "files": files,
        "truncated": budget_hit,
        "omitted_files": omitted_files,
        "skipped_files": skipped_files,
        "stats": {
            "files_returned": len(files),
            "files_omitted": len(omitted_files),
            "files_skipped": len(skipped_files),
            "total_additions": total_adds,
            "total_deletions": total_dels,
        },
    }
    if budget_hit:
        result["next"] = (
            f"Output hit the {max_chars}-char budget; {len(omitted_files)} file(s) "
            "were not expanded. Fetch them with "
            "mr_diff_sections(url, files_filter=[<file_path from omitted_files>, ...])."
        )
    return result


# ---------------------------------------------------------------------------
# Reusable changed-symbol extraction (shared with mr_review_context)
# ---------------------------------------------------------------------------

def changed_symbols(patch: str, language: str | None, is_new_file: bool) -> list[dict]:
    """The real changed declaration symbols in one file's patch.

    Reuses the exact section resolution mr_diff_sections uses (AST for new files,
    hunk-header + declaration patterns for modified files), so what
    mr_review_context analyzes matches what mr_diff_sections shows. Returns
    [{symbol, symbol_type, changed_lines}] for sections that resolved to a named
    symbol, ordered by changed_lines desc. Sections with no resolvable symbol are
    skipped — they aren't actionable review targets, and skipping them is what
    keeps stopwords/common tokens out of the changed-symbol list.
    """
    hunks = parse_hunks(patch)
    if not hunks:
        return []
    sections = _build_sections(patch, hunks, language, is_new_file, include_context_lines=False)

    # Sum changed lines per symbol (a symbol can span multiple sections).
    weight: dict[str, int] = {}
    kind: dict[str, str] = {}
    for s in sections:
        name = s.get("symbol")
        if not name:
            continue
        changed = sum(1 for c in s["changes"] if c["type"] in ("add", "del"))
        weight[name] = weight.get(name, 0) + changed
        kind.setdefault(name, s.get("symbol_type", "unknown"))

    out = [
        {"symbol": name, "symbol_type": kind[name], "changed_lines": n}
        for name, n in weight.items()
    ]
    out.sort(key=lambda d: d["changed_lines"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Section building
# ---------------------------------------------------------------------------

def _build_sections(
    patch: str,
    hunks: list[dict],
    language: str | None,
    is_new_file: bool,
    include_context_lines: bool,
) -> list[dict]:
    if not hunks:
        return []

    # For new files in a supported language, use full AST chunking
    if is_new_file and language:
        sections = _sections_from_ast(hunks, language, include_context_lines)
        if sections:
            return sections

    return _sections_from_hunk_context(patch, hunks, language, include_context_lines)


def _sections_from_ast(
    hunks: list[dict],
    language: str,
    include_context_lines: bool,
) -> list[dict]:
    """
    For new files: reconstruct content from all-add hunks, run AST chunker,
    then map each chunk's line range to the corresponding changed lines.
    """
    # Reconstruct file content from add lines (new file = 100% adds)
    lines_by_num: dict[int, str] = {}
    for hunk in hunks:
        for line in hunk["lines"]:
            if line["type"] == "add" and line["new_line"] is not None:
                lines_by_num[line["new_line"]] = line["content"]

    if not lines_by_num:
        return []

    max_line = max(lines_by_num.keys())
    full_content = "\n".join(lines_by_num.get(i, "") for i in range(1, max_line + 1))

    chunks = ast_chunk(full_content, language)
    if not chunks:
        return []

    sections: list[dict] = []
    for chunk_text, start_line, end_line, symbol, symbol_type in chunks:
        changes: list[dict] = []
        for hunk in hunks:
            for line in hunk["lines"]:
                ref = line.get("new_line") or line.get("old_line")
                if ref is None:
                    continue
                if start_line <= ref <= end_line:
                    if not include_context_lines and line["type"] == "context":
                        continue
                    changes.append(line)

        if not changes:
            continue

        sections.append({
            "symbol": symbol,
            "symbol_type": symbol_type,
            "start_line": start_line,
            "end_line": end_line,
            "changes": changes,
        })

    return sections


def _sections_from_hunk_context(
    patch: str,
    hunks: list[dict],
    language: str | None,
    include_context_lines: bool,
) -> list[dict]:
    """
    For modified files: group hunks by the enclosing symbol.

    Symbol detection priority:
      1. Text after @@ -x,y +x,y @@ in the hunk header (platforms often
         include the function signature there).
      2. The last declaration line seen in the hunk's context lines (scanning
         backwards from the first changed line).
    """
    decl_pattern = DECL_PATTERNS.get(language) if language else None
    hunk_ctx_hints = _parse_hunk_header_contexts(patch)

    sections: list[dict] = []

    for i, hunk in enumerate(hunks):
        hint = hunk_ctx_hints[i] if i < len(hunk_ctx_hints) else None
        symbol, symbol_type = _resolve_symbol(hunk, decl_pattern, hint)

        # Collect lines for this section
        changes: list[dict] = []
        new_nums: list[int] = []
        old_nums: list[int] = []

        for line in hunk["lines"]:
            if not include_context_lines and line["type"] == "context":
                continue
            changes.append(line)
            if line.get("new_line"):
                new_nums.append(line["new_line"])
            if line.get("old_line"):
                old_nums.append(line["old_line"])

        if not changes:
            continue

        start_line = (
            min(new_nums) if new_nums else (min(old_nums) if old_nums else hunk["new_start"])
        )
        end_line = (
            max(new_nums) if new_nums else (max(old_nums) if old_nums else hunk["new_start"])
        )

        # Merge consecutive hunks in the same symbol to avoid fragmentation
        if (
            sections
            and symbol is not None
            and sections[-1]["symbol"] == symbol
        ):
            sections[-1]["changes"].extend(changes)
            sections[-1]["end_line"] = end_line
        else:
            sections.append({
                "symbol": symbol,
                "symbol_type": symbol_type,
                "start_line": start_line,
                "end_line": end_line,
                "changes": changes,
            })

    return sections


# ---------------------------------------------------------------------------
# Symbol resolution helpers
# ---------------------------------------------------------------------------

def _parse_hunk_header_contexts(patch: str) -> list[str | None]:
    """Extract the function-context text appended after each @@ header."""
    contexts: list[str | None] = []
    for line in patch.splitlines():
        m = _HUNK_HDR_RE.match(line)
        if m:
            ctx = m.group(1)
            contexts.append(ctx.strip() if ctx else None)
    return contexts


def _resolve_symbol(
    hunk: dict,
    decl_pattern: re.Pattern | None,
    hint: str | None,
) -> tuple[str | None, str]:
    """
    Return (symbol_name, symbol_type) for a hunk.

    Tries hunk-header hint first, then scans backwards through context lines
    for the nearest declaration.
    """
    # 1. Hunk header hint (e.g. "fun showAirportPickUpBottomSheet()" from @@)
    if hint:
        name = _name_from_text(hint)
        if name:
            return name, _type_from_text(hint)

    # 2. Scan context lines from first changed line upward
    if decl_pattern:
        # Collect context lines that appear before the first add/del
        pre_context: list[str] = []
        for line in hunk["lines"]:
            if line["type"] != "context":
                break
            pre_context.append(line["content"])

        # Search in reverse (nearest declaration wins)
        for content in reversed(pre_context):
            m = decl_pattern.match(content)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name:
                    return name, _type_from_text(content)

        # Also scan all context lines (declaration may appear after a change)
        for line in hunk["lines"]:
            if line["type"] == "context" and decl_pattern.match(line["content"]):
                m = decl_pattern.match(line["content"])
                if m:
                    name = next((g for g in m.groups() if g), None)
                    if name:
                        return name, _type_from_text(line["content"])

    return None, "unknown"


def _name_from_text(text: str) -> str | None:
    """Extract the declared symbol name from a hint or declaration line."""
    m = _KEYWORD_NAME_RE.search(text)
    if m:
        return m.group(1)
    m = _MODIFIER_NAME_RE.search(text)
    if m:
        return m.group(1)
    return None


def _type_from_text(text: str) -> str:
    """Classify symbol type from declaration text."""
    t = text.lower()
    if "class" in t or "object" in t or "struct" in t or "impl" in t:
        return "class"
    if "interface" in t or "trait" in t:
        return "interface"
    if "enum" in t:
        return "enum"
    if any(k in t for k in ("fun ", "def ", "func ", "function ")):
        return "function"
    return "other"
