"""
Declaration patterns used to find the enclosing function/class of a changed
line from surrounding context lines in a diff.

Used by mr_diff_sections to resolve the symbol name for hunks in modified
files, when the hunk header itself doesn't already carry a function-context
hint.
"""

from __future__ import annotations

import re

DECL_PATTERNS: dict[str, re.Pattern] = {
    "kotlin": re.compile(
        r"^\s*(?:(?:override|private|internal|public|protected|"
        r"suspend|inline|abstract|open|sealed|data|fun)\s+)*fun\s+(\w+)"
        r"|^\s*(?:(?:abstract|open|sealed|data|inner|private|internal|"
        r"public|enum)\s+)*class\s+(\w+)"
        r"|^\s*(?:private\s+)?object\s+(\w+)"
        r"|^\s*interface\s+(\w+)"
    ),
    "java": re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|abstract|"
        r"synchronized|native|strictfp)\s+)*"
        r"(?:void|boolean|int|long|double|float|String|[A-Z]\w*)(?:<[^>]+>)?\s+(\w+)\s*\("
        r"|^\s*(?:(?:public|private|protected|abstract|final)\s+)*class\s+(\w+)"
        r"|^\s*interface\s+(\w+)"
    ),
    "python": re.compile(
        r"^\s*(?:async\s+)?def\s+(\w+)"
        r"|^\s*class\s+(\w+)"
    ),
    "javascript": re.compile(
        r"^\s*(?:async\s+)?function\s+(\w+)"
        r"|^\s*class\s+(\w+)"
        r"|^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\("
    ),
    "typescript": re.compile(
        r"^\s*(?:async\s+)?function\s+(\w+)"
        r"|^\s*(?:export\s+)?class\s+(\w+)"
        r"|^\s*interface\s+(\w+)"
        r"|^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\("
    ),
    "go": re.compile(
        r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\("
    ),
    "rust": re.compile(
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"
        r"|^\s*(?:pub\s+)?impl(?:\s+\w+)?\s+(?:for\s+)?(\w+)"
        r"|^\s*(?:pub\s+)?struct\s+(\w+)"
        r"|^\s*(?:pub\s+)?enum\s+(\w+)"
        r"|^\s*(?:pub\s+)?trait\s+(\w+)"
    ),
}
