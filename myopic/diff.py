"""
Unified-diff parsing — platform-agnostic.

A unified diff is a unified diff whether it comes from a GitLab MR or a GitHub
PR, so this module knows nothing about either. It turns raw patch text into
structured hunks with exact old/new line numbers, which is what makes precise
inline-comment posting possible.
"""

from __future__ import annotations

import re

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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
