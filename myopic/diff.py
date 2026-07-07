"""
Unified-diff parsing — platform-agnostic.

A unified diff is a unified diff whether it comes from a GitLab MR or a GitHub
PR, so this module knows nothing about either. It turns raw patch text into
structured hunks with exact old/new line numbers, which is what makes precise
inline-comment posting possible.
"""

from __future__ import annotations

import re
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Extensions -> language for AST-based tools (mr_diff_sections, dependency_impact,
# trace_call_chain). Only includes languages with tree-sitter support in
# myopic/ast_chunker.py.
EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin", ".go": "go", ".rs": "rust",
}

# Directories to skip during filesystem walks (dependency_impact, trace_call_chain).
# Superset across all tools — safe to use anywhere.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".gradle", ".idea", "target", ".mypy_cache",
    "coverage", ".expo", "Pods", "DerivedData", ".ruff_cache",
    "coding_agent.egg-info", ".pytest_cache",
})

# ---------------------------------------------------------------------------
# File classification — separate reviewer-noise from real code changes.
#
# The key to reviewing large MRs without overflowing context: noise files
# (lockfiles, generated code, vendored/build artifacts, binary assets, or an
# enormous machine-generated change) are still *listed*, just kept out of the
# diff body unless explicitly requested. Platform-agnostic — operates on a path.
# ---------------------------------------------------------------------------

_LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "composer.lock", "gemfile.lock", "poetry.lock", "pdm.lock", "cargo.lock",
    "go.sum", "packages.lock.json", "podfile.lock", "flake.lock", "uv.lock",
}
_GENERATED_SUFFIXES = (
    ".min.js", ".min.css", ".map",
    ".pb.go", "_pb2.py", "_pb2_grpc.py", ".pb.cc", ".pb.h",
    ".g.dart", ".freezed.dart", ".g.kt", ".generated.ts", ".generated.js",
)
_VENDOR_DIR_MARKERS = (
    "/node_modules/", "/vendor/", "/dist/", "/build/", "/.next/", "/out/",
    "/target/", "/__generated__/", "/generated/", "/third_party/", "/.gradle/",
)
_BINARY_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".zip", ".gz", ".tar",
    ".jar", ".class", ".so", ".dll", ".dylib", ".bin", ".snap",
)
# A single-file change bigger than this is almost always machine-generated.
_HUGE_CHANGE_LINES = 2000


def classify_file(path: str, additions: int = 0, deletions: int = 0) -> tuple[bool, str | None]:
    """Classify a changed file as reviewable code vs reviewer-noise.

    Returns (reviewable, skip_reason); skip_reason is None when reviewable.
    Never drops a file — it only lets callers keep noise out of the token-bounded
    diff body while still listing it in the manifest.
    """
    p = path.lower()
    name = Path(p).name
    marker = "/" + p  # so a top-level "vendor/x" still matches "/vendor/"

    if name in _LOCKFILE_NAMES:
        return False, "lockfile"
    if p.endswith(_GENERATED_SUFFIXES):
        return False, "generated"
    if any(m in marker for m in _VENDOR_DIR_MARKERS):
        return False, "vendored/build artifact"
    if p.endswith(_BINARY_ASSET_SUFFIXES):
        return False, "binary/asset"
    if additions + deletions > _HUGE_CHANGE_LINES:
        return False, f"very large change ({additions + deletions} lines, likely generated)"
    return True, None


def count_lines(patch: str) -> tuple[int, int]:
    """Count additions and deletions in a unified-diff patch."""
    additions = sum(
        1 for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return additions, deletions


def parse_hunks(patch: str) -> list[dict]:
    """
    Parse a unified-diff patch into structured hunks with exact line numbers.

    Each line entry carries:
      - type:     "add" | "del" | "context"
      - old_line: line number in the old file (None for additions)
      - new_line: line number in the new file (None for deletions)
      - content:  the line content with its leading +/-/space stripped
    """
    hunks: list[dict] = []
    current: dict | None = None
    old_line = 0
    new_line = 0

    for raw in patch.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            current = {
                "old_start": int(match.group(1)),
                "old_lines": int(match.group(2) or 1),
                "new_start": int(match.group(3)),
                "new_lines": int(match.group(4) or 1),
                "lines": [],
            }
            hunks.append(current)
            old_line = current["old_start"]
            new_line = current["new_start"]
            continue

        if current is None:
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            current["lines"].append({
                "type": "add",
                "old_line": None,
                "new_line": new_line,
                "content": raw[1:],
            })
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current["lines"].append({
                "type": "del",
                "old_line": old_line,
                "new_line": None,
                "content": raw[1:],
            })
            old_line += 1
        elif raw.startswith("\\"):
            # "\ No newline at end of file" — not a real line, skip it.
            continue
        else:
            content = raw[1:] if raw.startswith(" ") else raw
            current["lines"].append({
                "type": "context",
                "old_line": old_line,
                "new_line": new_line,
                "content": content,
            })
            old_line += 1
            new_line += 1

    return hunks


def find_line_mappings(hunks: list[dict], target_lines: list[int]) -> list[dict]:
    """
    For each requested new-file line number, find the closest diff line.

    Prefers an exact new_line match; otherwise falls back to the nearest line
    (by new_line distance) that has a new_line value. Returns one entry per
    requested line, sorted ascending. Used to translate "I want to comment on
    source line N" into the diff position the platform API actually requires.
    """
    all_lines = [line for hunk in hunks for line in hunk["lines"]]
    # Only lines with a new_line (add/context) can anchor a new-side comment.
    new_side = [line for line in all_lines if line["new_line"] is not None]

    mappings: list[dict] = []
    for target in sorted(set(target_lines)):
        if not new_side:
            mappings.append({
                "requested": target, "new_line": None, "old_line": None,
                "type": None, "content": None, "exact": False,
            })
            continue

        exact = next((l for l in new_side if l["new_line"] == target), None)
        chosen = exact or min(new_side, key=lambda l: abs(l["new_line"] - target))
        mappings.append({
            "requested": target,
            "new_line": chosen["new_line"],
            "old_line": chosen["old_line"],
            "type": chosen["type"],
            "content": chosen["content"],
            "exact": exact is not None,
        })

    return mappings
