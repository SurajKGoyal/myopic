"""
trace_call_chain — trace callers and callees of a function/class via tree-sitter AST.

Given a symbol name, finds:
- Where it's defined
- What it calls (callees)
- What calls it (callers)

Filesystem-based (operates on a local repo path, not a review URL) and
platform-agnostic — never touches GitLab. No LLM — pure AST analysis.
"""

from __future__ import annotations

from pathlib import Path

from myopic.diff import EXT_TO_LANG, SKIP_DIRS

# Node types that represent function/method calls by language
_CALL_NODES = {
    "python": {"call"},
    "javascript": {"call_expression"},
    "typescript": {"call_expression"},
    "java": {"method_invocation", "object_creation_expression"},
    "kotlin": {"call_expression"},
    "go": {"call_expression"},
    "rust": {"call_expression"},
}

# Node types that define functions/methods/classes
_DEF_NODES = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {"function_declaration", "class_declaration", "method_definition"},
    "java": {"method_declaration", "class_declaration", "constructor_declaration"},
    "kotlin": {"function_declaration", "class_declaration"},
    "go": {"function_declaration", "method_declaration"},
    "rust": {"function_item", "impl_item"},
}

MAX_FILES = 500


def trace_call_chain(
    symbol: str,
    root: str,
    language: str | None = None,
    max_depth: int = 1,
) -> dict:
    """
    Trace callers and callees of a symbol across a repository.

    Args:
        symbol: Function or class name to trace.
        root: Absolute path to the repository to search.
        language: Filter to a specific language (auto-detects if None).
        max_depth: Levels of callers/callees to trace (default 1).

    Returns:
    {
        "symbol": str,
        "definition": {"file_path": str, "line": int, "type": str} | null,
        "callees": [{"name": str, "file_path": str, "line": int}],
        "callers": [{"name": str, "file_path": str, "line": int, "call_line": int}],
        "stats": {"files_scanned": int, "parse_errors": int}
    }
    """
    repo_root = Path(root).resolve()
    if not repo_root.exists():
        return {"error": f"Path does not exist: {repo_root}"}

    # Collect files to scan
    files = _collect_files(repo_root, language)
    if not files:
        return {"error": f"No source files found in {repo_root}"}

    definition = None
    callees = []
    callers = []
    parse_errors = 0

    for file_path, lang in files[:MAX_FILES]:
        parsed = _parse_file(file_path, lang)
        if parsed is None:
            parse_errors += 1
            continue

        tree, parser, config = parsed
        rel_path = str(file_path.relative_to(repo_root))

        # Find definition
        if definition is None:
            defn = _find_definition(tree.root_node, symbol, lang, rel_path)
            if defn:
                definition = defn
                # Extract callees from the definition node
                callees = _find_callees(tree.root_node, symbol, lang, rel_path)

        # Find callers (in all files)
        file_callers = _find_callers(tree.root_node, symbol, lang, rel_path)
        callers.extend(file_callers)

    # Deduplicate callers
    seen = set()
    unique_callers = []
    for c in callers:
        key = (c["file_path"], c["call_line"])
        if key not in seen:
            seen.add(key)
            unique_callers.append(c)

    return {
        "symbol": symbol,
        "definition": definition,
        "callees": callees,
        "callers": unique_callers,
        "stats": {
            "files_scanned": min(len(files), MAX_FILES),
            "parse_errors": parse_errors,
        },
    }


def _collect_files(root: Path, language: str | None) -> list[tuple[Path, str]]:
    """Collect source files with their detected language."""
    files = []
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if any(part in SKIP_DIRS for part in file_path.parts):
            continue
        if file_path.stat().st_size > 200_000:
            continue

        ext = file_path.suffix.lower()
        lang = EXT_TO_LANG.get(ext)
        if lang is None:
            continue
        if language and lang != language:
            continue

        files.append((file_path, lang))

    return files


def _parse_file(file_path: Path, language: str):
    """Parse a file with tree-sitter. Returns (tree, parser, config) or None."""
    from myopic.ast_chunker import _get_parser

    result = _get_parser(language)
    if result is None:
        return None

    parser, config = result

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = parser.parse(content.encode("utf-8"))
        return tree, parser, config
    except Exception:
        return None


def _find_definition(root_node, symbol: str, language: str, rel_path: str) -> dict | None:
    """Find where the symbol is defined."""
    def_nodes = _DEF_NODES.get(language, set())

    def _walk(node):
        if node.type in def_nodes:
            name = _get_name(node)
            if name == symbol:
                return {
                    "file_path": rel_path,
                    "line": node.start_point[0] + 1,
                    "type": _classify_def_type(node.type),
                }
            # Check inside decorated_definition
            if node.type == "decorated_definition":
                for child in node.children:
                    if child.type in def_nodes:
                        child_name = _get_name(child)
                        if child_name == symbol:
                            return {
                                "file_path": rel_path,
                                "line": child.start_point[0] + 1,
                                "type": _classify_def_type(child.type),
                            }

        for child in node.children:
            result = _walk(child)
            if result:
                return result
        return None

    return _walk(root_node)


def _find_callees(root_node, symbol: str, language: str, rel_path: str) -> list[dict]:
    """Find all functions called inside the symbol's definition."""
    def_nodes = _DEF_NODES.get(language, set())
    call_nodes = _CALL_NODES.get(language, set())
    callees = []

    def _find_def_node(node):
        """Find the AST node for the symbol's definition."""
        if node.type in def_nodes:
            name = _get_name(node)
            if name == symbol:
                return node
            if node.type == "decorated_definition":
                for child in node.children:
                    if child.type in def_nodes and _get_name(child) == symbol:
                        return child
        for child in node.children:
            result = _find_def_node(child)
            if result:
                return result
        return None

    def_node = _find_def_node(root_node)
    if not def_node:
        return callees

    def _collect_calls(node):
        if node.type in call_nodes:
            name = _get_call_name(node)
            if name and name != symbol:  # Skip recursive calls
                callees.append({
                    "name": name,
                    "file_path": rel_path,
                    "line": node.start_point[0] + 1,
                })
        for child in node.children:
            _collect_calls(child)

    _collect_calls(def_node)

    # Deduplicate by name
    seen = set()
    unique = []
    for c in callees:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)

    return unique


def _find_callers(root_node, symbol: str, language: str, rel_path: str) -> list[dict]:
    """Find all functions that call the symbol."""
    def_nodes = _DEF_NODES.get(language, set())
    call_nodes = _CALL_NODES.get(language, set())
    callers = []

    def _walk(node, enclosing_name: str | None = None):
        current_enclosing = enclosing_name

        # Track enclosing function/method
        if node.type in def_nodes:
            name = _get_name(node)
            if name:
                current_enclosing = name

        # Check if this is a call to our symbol
        if node.type in call_nodes:
            call_name = _get_call_name(node)
            if call_name == symbol:
                callers.append({
                    "name": current_enclosing or "<module>",
                    "file_path": rel_path,
                    "line": node.start_point[0] + 1,  # enclosing start
                    "call_line": node.start_point[0] + 1,
                })

        for child in node.children:
            _walk(child, current_enclosing)

    _walk(root_node)
    return callers


def _get_name(node) -> str | None:
    """Extract the name from a definition node."""
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
            return child.text.decode("utf-8", errors="replace")
    # For decorated_definition, look deeper
    for child in node.children:
        if "definition" in child.type or "declaration" in child.type:
            return _get_name(child)
    return None


def _get_call_name(node) -> str | None:
    """Extract the function/method name from a call node."""
    # Direct call: foo()
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8", errors="replace")

    # Method call: obj.method() — get the method name
    for child in node.children:
        if child.type in ("member_expression", "attribute", "field_access", "field_expression"):
            # Get the last identifier (the method name)
            for sub in child.children:
                if sub.type in ("property_identifier", "identifier", "field_identifier"):
                    return sub.text.decode("utf-8", errors="replace")

    # Java method_invocation: first child is often the method name
    if node.type == "method_invocation":
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8", errors="replace")

    return None


def _classify_def_type(node_type: str) -> str:
    """Classify a definition node type."""
    if "class" in node_type:
        return "class"
    if "method" in node_type or "constructor" in node_type:
        return "method"
    if "function" in node_type:
        return "function"
    if "impl" in node_type:
        return "impl"
    return "other"
