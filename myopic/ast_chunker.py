"""
AST-aware code chunking using tree-sitter.

Splits code by semantic boundaries (functions, classes, methods) instead of
fixed line windows. Falls back gracefully when tree-sitter grammars are
unavailable or parsing fails.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 4_000

# Mapping from our language names to tree-sitter module names + node types
_LANGUAGE_CONFIG: dict[str, dict] = {
    "python": {
        "module": "tree_sitter_python",
        "top_nodes": {
            "function_definition", "class_definition", "decorated_definition",
        },
        "sub_nodes": {"function_definition"},  # methods inside classes
    },
    "javascript": {
        "module": "tree_sitter_javascript",
        "top_nodes": {
            "function_declaration", "class_declaration", "export_statement",
            "lexical_declaration", "expression_statement",
        },
        "sub_nodes": {"method_definition", "function_declaration"},
    },
    "typescript": {
        "module": "tree_sitter_typescript",
        "lang_attr": "language_typescript",
        "top_nodes": {
            "function_declaration", "class_declaration", "export_statement",
            "lexical_declaration", "interface_declaration", "type_alias_declaration",
            "expression_statement", "enum_declaration",
        },
        "sub_nodes": {"method_definition", "function_declaration"},
    },
    "java": {
        "module": "tree_sitter_java",
        "top_nodes": {
            "class_declaration", "interface_declaration", "enum_declaration",
            "method_declaration", "import_declaration",
        },
        "sub_nodes": {"method_declaration", "constructor_declaration"},
    },
    "go": {
        "module": "tree_sitter_go",
        "top_nodes": {
            "function_declaration", "method_declaration", "type_declaration",
        },
        "sub_nodes": set(),
    },
    "rust": {
        "module": "tree_sitter_rust",
        "top_nodes": {
            "function_item", "impl_item", "struct_item", "enum_item",
            "trait_item", "mod_item",
        },
        "sub_nodes": {"function_item"},
    },
}

# Cache parsed languages to avoid re-importing
_parser_cache: dict[str, object] = {}


def _get_parser(language: str):
    """Get or create a tree-sitter parser for the given language."""
    if language in _parser_cache:
        return _parser_cache[language]

    config = _LANGUAGE_CONFIG.get(language)
    if not config:
        return None

    try:
        import importlib

        import tree_sitter

        mod = importlib.import_module(config["module"])
        lang_attr = config.get("lang_attr", "language")
        lang_fn = getattr(mod, lang_attr, None)
        if lang_fn is None:
            # some grammars export language() as a function
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return None

        raw_lang = lang_fn()
        # tree-sitter >=0.24 returns PyCapsule from language(), wrap it
        if not isinstance(raw_lang, tree_sitter.Language):
            lang_obj = tree_sitter.Language(raw_lang)
        else:
            lang_obj = raw_lang
        parser = tree_sitter.Parser(lang_obj)
        _parser_cache[language] = (parser, config)
        return (parser, config)
    except (ImportError, AttributeError, Exception) as e:
        logger.debug("tree-sitter unavailable for %s: %s", language, e)
        _parser_cache[language] = None
        return None


def _extract_symbol_name(node) -> Optional[str]:
    """Extract the name of a function/class/method from an AST node."""
    # Look for a 'name' or 'identifier' child
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
            return child.text.decode("utf-8", errors="replace")
    # For decorated_definition / export_statement, recurse into the actual definition
    for child in node.children:
        if "definition" in child.type or "declaration" in child.type:
            return _extract_symbol_name(child)
    return None


def _node_symbol_type(node) -> str:
    """Classify a node as 'class', 'function', 'method', 'interface', or 'other'."""
    t = node.type
    if "class" in t:
        return "class"
    if "interface" in t:
        return "interface"
    if "method" in t or "constructor" in t:
        return "method"
    if "function" in t:
        return "function"
    if "enum" in t:
        return "enum"
    if "impl" in t or "trait" in t:
        return "trait"
    return "other"


def ast_chunk(
    content: str,
    language: str,
) -> Optional[list[tuple[str, int, int, Optional[str], str]]]:
    """
    Split content by AST boundaries.

    Returns list of (chunk_text, start_line_1indexed, end_line_1indexed, symbol_name, symbol_type)
    or None if tree-sitter is unavailable / parsing fails.
    """
    result = _get_parser(language)
    if result is None:
        return None

    parser, config = result
    top_nodes = config["top_nodes"]
    sub_nodes = config.get("sub_nodes", set())

    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception as e:
        logger.debug("tree-sitter parse failed for %s: %s", language, e)
        return None

    lines = content.split("\n")
    chunks: list[tuple[str, int, int, Optional[str], str]] = []

    def _extract_node(node, is_sub: bool = False):
        """Extract a node as a chunk, potentially splitting large nodes."""
        start_line = node.start_point[0]  # 0-indexed
        end_line = node.end_point[0]  # 0-indexed
        text = "\n".join(lines[start_line:end_line + 1])
        name = _extract_symbol_name(node)
        sym_type = _node_symbol_type(node)

        if len(text) <= MAX_CHUNK_CHARS:
            chunks.append((text, start_line + 1, end_line + 1, name, sym_type))
            return

        # Large node — try to split by sub-nodes (e.g., methods in a class)
        if not is_sub and sub_nodes:
            sub_children = [c for c in node.children if c.type in sub_nodes]
            if sub_children:
                # Add class header (everything before first method)
                first_sub_start = sub_children[0].start_point[0]
                if first_sub_start > start_line:
                    header = "\n".join(lines[start_line:first_sub_start])
                    if header.strip() and len(header) <= MAX_CHUNK_CHARS:
                        chunks.append((header, start_line + 1, first_sub_start, name, sym_type))

                for sub in sub_children:
                    _extract_node(sub, is_sub=True)
                return

        # Fallback: split oversized node into MAX_CHUNK_CHARS pieces
        chunk_lines = []
        chunk_start = start_line
        current_len = 0
        for i in range(start_line, end_line + 1):
            line = lines[i] if i < len(lines) else ""
            if current_len + len(line) + 1 > MAX_CHUNK_CHARS and chunk_lines:
                chunks.append((
                    "\n".join(chunk_lines),
                    chunk_start + 1,
                    chunk_start + len(chunk_lines),
                    name,
                    sym_type,
                ))
                chunk_lines = []
                chunk_start = i
                current_len = 0
            chunk_lines.append(line)
            current_len += len(line) + 1

        if chunk_lines:
            chunks.append((
                "\n".join(chunk_lines),
                chunk_start + 1,
                chunk_start + len(chunk_lines),
                name,
                sym_type,
            ))

    # Collect top-level nodes
    root = tree.root_node
    collected_ranges: list[tuple[int, int]] = []

    for child in root.children:
        if child.type in top_nodes:
            _extract_node(child)
            collected_ranges.append((child.start_point[0], child.end_point[0]))

    # Merge uncollected lines (imports, module-level code) into a preamble chunk
    if collected_ranges and lines:
        collected_ranges.sort()
        uncollected_lines = []
        uncollected_start = 0

        for rng_start, rng_end in collected_ranges:
            for i in range(uncollected_start, rng_start):
                if i < len(lines) and lines[i].strip():
                    uncollected_lines.append((i, lines[i]))
            uncollected_start = rng_end + 1

        # Trailing lines after last collected node
        for i in range(uncollected_start, len(lines)):
            if lines[i].strip():
                uncollected_lines.append((i, lines[i]))

        if uncollected_lines:
            preamble_text = "\n".join(l[1] for l in uncollected_lines)
            if len(preamble_text) <= MAX_CHUNK_CHARS and preamble_text.strip():
                first_line = uncollected_lines[0][0] + 1
                last_line = uncollected_lines[-1][0] + 1
                chunks.append((preamble_text, first_line, last_line, None, "preamble"))

    if not chunks:
        return None

    # Sort by start line
    chunks.sort(key=lambda c: c[1])
    return chunks
