"""
dependency_impact — find all files/functions referencing a given symbol.

Two-stage pipeline, platform-agnostic and filesystem-based (operates on a
local repo path, not a review URL):
1. ripgrep for fast candidate finding (word-boundary matching), falling back
   to grep when ripgrep isn't installed.
2. tree-sitter AST validation to classify each usage (call / import /
   definition / type_reference / assignment / reference).

No LLM — pure computation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from myopic.diff import EXT_TO_LANG, SKIP_DIRS


def dependency_impact(
    symbol: str,
    root: str,
    file_glob: str | None = None,
    whole_word: bool = True,
    max_results: int = 50,
) -> dict:
    """
    Find all references to a symbol in a repository.

    Uses ripgrep for fast search, then classifies each usage via AST
    when tree-sitter is available for the file's language.

    Args:
        symbol: The symbol to search for (function, class, variable name).
        root: Absolute path to the repository to search.
        file_glob: Only search files matching this glob (e.g., "*.java", "src/**/*.ts").
        whole_word: Match whole words only (default True).
        max_results: Maximum references to return (default 50).

    Returns:
    {
        "symbol": str,
        "root": str,
        "total_references": int,
        "references": [
            {
                "file_path": str,
                "line": int,
                "context": str,
                "usage_type": str,
                "enclosing_symbol": str | null
            }
        ],
        "by_file": {"path/to/file.java": count},
        "by_usage_type": {"call": count, "import": count, ...}
    }
    """
    repo_root = Path(root).resolve()
    if not repo_root.exists():
        return {"error": f"Path does not exist: {repo_root}"}

    # Stage 1: ripgrep for candidates
    candidates = _ripgrep_search(symbol, repo_root, file_glob, whole_word, max_results * 2)
    if isinstance(candidates, dict) and "error" in candidates:
        return candidates

    # Stage 2: AST classification
    references = []
    file_counts: Counter = Counter()
    type_counts: Counter = Counter()

    for candidate in candidates[:max_results]:
        file_path = candidate["file_path"]
        line_num = candidate["line"]
        context = candidate["context"]

        # Classify usage type
        usage_type = "reference"
        enclosing = None

        abs_path = repo_root / file_path
        ext = abs_path.suffix.lower()
        lang = EXT_TO_LANG.get(ext)

        if lang:
            classification = _classify_with_ast(abs_path, line_num, symbol, lang)
            if classification:
                usage_type = classification["usage_type"]
                enclosing = classification.get("enclosing_symbol")

        references.append({
            "file_path": file_path,
            "line": line_num,
            "context": context.strip(),
            "usage_type": usage_type,
            "enclosing_symbol": enclosing,
        })

        file_counts[file_path] += 1
        type_counts[usage_type] += 1

    return {
        "symbol": symbol,
        "root": str(repo_root),
        "total_references": len(references),
        "references": references,
        "by_file": dict(file_counts.most_common()),
        "by_usage_type": dict(type_counts.most_common()),
    }


def _ripgrep_search(
    symbol: str, root: Path, file_glob: str | None, whole_word: bool, limit: int
) -> list[dict] | dict:
    """Fast search using ripgrep, falling back to grep."""
    rg = shutil.which("rg")

    if rg:
        cmd = [rg, "--json", "-n", "--max-count", "5"]
        if whole_word:
            cmd.append("-w")
        for skip_dir in SKIP_DIRS:
            cmd.extend(["--glob", f"!{skip_dir}"])
        if file_glob:
            cmd.extend(["--glob", file_glob])
        cmd.extend([symbol, str(root)])
    else:
        # Fallback to grep
        cmd = ["grep", "-rn"]
        if whole_word:
            cmd.append("-w")
        for skip_dir in SKIP_DIRS:
            cmd.extend(["--exclude-dir", skip_dir])
        if file_glob:
            cmd.extend(["--include", file_glob])
        cmd.extend([symbol, str(root)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Search timed out after 30s"}
    except Exception as e:
        return {"error": f"Search failed: {e}"}

    candidates = []

    if rg:
        # Parse ripgrep JSON output
        for line in result.stdout.splitlines():
            try:
                data = json.loads(line)
                if data.get("type") != "match":
                    continue
                match_data = data["data"]
                file_path = match_data["path"]["text"]
                # Make relative to root
                try:
                    file_path = str(Path(file_path).relative_to(root))
                except ValueError:
                    pass
                line_num = match_data["line_number"]
                context = match_data["lines"]["text"]
                candidates.append({
                    "file_path": file_path,
                    "line": line_num,
                    "context": context,
                })
            except (json.JSONDecodeError, KeyError):
                continue
    else:
        # Parse grep output: file:line:content
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path = parts[0]
            try:
                file_path = str(Path(file_path).relative_to(root))
            except ValueError:
                pass
            try:
                line_num = int(parts[1])
            except ValueError:
                continue
            candidates.append({
                "file_path": file_path,
                "line": line_num,
                "context": parts[2],
            })

    return candidates[:limit]


def _classify_with_ast(
    file_path: Path, line_num: int, symbol: str, language: str
) -> dict | None:
    """Classify a symbol usage at a specific line using tree-sitter AST."""
    from myopic.ast_chunker import _get_parser

    result = _get_parser(language)
    if result is None:
        return None

    parser, config = result

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return None

    target_line = line_num - 1  # 0-indexed

    # Find the node at the target line containing the symbol
    usage_type = _classify_node_at_line(tree.root_node, target_line, symbol, language)

    # Find enclosing function/class
    enclosing = _find_enclosing_symbol(tree.root_node, target_line, config)

    return {
        "usage_type": usage_type,
        "enclosing_symbol": enclosing,
    }


def _classify_node_at_line(root_node, target_line: int, symbol: str, language: str) -> str:
    """Walk AST to classify what kind of usage the symbol has at this line."""
    # Collect all nodes that span the target line
    nodes_at_line = []
    _collect_nodes_at_line(root_node, target_line, nodes_at_line)

    for node in nodes_at_line:
        node_type = node.type

        # Import detection
        if "import" in node_type:
            return "import"

        # Definition detection
        if node_type in (
            "function_definition", "function_declaration", "method_declaration",
            "class_definition", "class_declaration", "interface_declaration",
            "method_definition", "constructor_declaration",
        ):
            # Check if the symbol is the name being defined
            for child in node.children:
                if child.type in ("identifier", "name", "type_identifier"):
                    text = child.text.decode("utf-8", errors="replace")
                    if text == symbol:
                        return "definition"

        # Call detection
        if node_type in ("call", "call_expression", "method_invocation"):
            return "call"

        # Type reference (Java/Kotlin/TS)
        if node_type in (
            "type_identifier", "generic_type", "type_annotation",
            "object_creation_expression",
        ):
            return "type_reference"

        # Assignment
        if node_type in ("assignment", "variable_declarator", "local_variable_declaration"):
            return "assignment"

    return "reference"


def _collect_nodes_at_line(node, target_line: int, result: list):
    """Collect all AST nodes that span the target line."""
    if node.start_point[0] <= target_line <= node.end_point[0]:
        result.append(node)
        for child in node.children:
            _collect_nodes_at_line(child, target_line, result)


def _find_enclosing_symbol(root_node, target_line: int, config: dict) -> str | None:
    """Find the name of the function/class/method enclosing the target line."""
    top_nodes = config["top_nodes"]
    sub_nodes = config.get("sub_nodes", set())
    all_nodes = top_nodes | sub_nodes

    best = None
    best_size = float("inf")

    def _walk(node):
        nonlocal best, best_size
        if node.type in all_nodes:
            if node.start_point[0] <= target_line <= node.end_point[0]:
                size = node.end_point[0] - node.start_point[0]
                if size < best_size:
                    from myopic.ast_chunker import _extract_symbol_name
                    name = _extract_symbol_name(node)
                    if name:
                        best = name
                        best_size = size
        for child in node.children:
            _walk(child)

    _walk(root_node)
    return best
