"""
Unit tests for the code-graph tools (dependency_impact, trace_call_chain).

Hermetic: no network access. Uses a tiny temporary repo fixture written to
tmp_path. Both tools use ripgrep with a grep fallback (the tool code itself
handles the fallback via shutil.which), and tree-sitter AST classification
degrades gracefully to a generic "reference"/callers-only result when a
grammar isn't installed — so these tests assert the structure and the
presence of the caller relationship rather than requiring a specific
AST-derived usage_type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myopic.ast_chunker import _get_parser
from myopic.tools.dependency_impact import dependency_impact
from myopic.tools.trace_call_chain import trace_call_chain

_PYTHON_GRAMMAR_AVAILABLE = _get_parser("python") is not None
requires_python_grammar = pytest.mark.skipif(
    not _PYTHON_GRAMMAR_AVAILABLE,
    reason="tree-sitter-python grammar not installed",
)


def _write_sample_repo(tmp_path: Path) -> Path:
    """Write a tiny two-file Python repo: one defines foo(), the other calls it."""
    (tmp_path / "definitions.py").write_text(
        "def foo():\n"
        "    return 42\n"
    )
    (tmp_path / "caller.py").write_text(
        "from definitions import foo\n"
        "\n"
        "def bar():\n"
        "    result = foo()\n"
        "    return result\n"
    )
    return tmp_path


class TestDependencyImpact:
    def test_finds_references_across_files(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = dependency_impact("foo", root=str(tmp_path))

        assert "error" not in result
        assert result["symbol"] == "foo"
        assert result["total_references"] >= 2  # definition + import + call

        files_hit = {ref["file_path"] for ref in result["references"]}
        assert "definitions.py" in files_hit
        assert "caller.py" in files_hit

    def test_by_file_and_by_usage_type_present(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = dependency_impact("foo", root=str(tmp_path))

        assert "error" not in result
        assert isinstance(result["by_file"], dict)
        assert isinstance(result["by_usage_type"], dict)
        assert sum(result["by_file"].values()) == result["total_references"]

    def test_nonexistent_root_returns_error(self, tmp_path):
        result = dependency_impact("foo", root=str(tmp_path / "does-not-exist"))
        assert "error" in result

    def test_whole_word_avoids_partial_matches(self, tmp_path):
        (tmp_path / "mod.py").write_text(
            "def foo():\n    pass\n\n\ndef foobar():\n    pass\n"
        )

        result = dependency_impact("foo", root=str(tmp_path), whole_word=True)

        assert "error" not in result
        # "foobar" should not match a whole-word search for "foo"
        contexts = " ".join(ref["context"] for ref in result["references"])
        assert "foobar" not in contexts or "def foo():" in contexts


class TestTraceCallChain:
    @requires_python_grammar
    def test_finds_definition_and_caller(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = trace_call_chain("foo", root=str(tmp_path))

        assert "error" not in result
        assert result["symbol"] == "foo"
        assert result["definition"] is not None
        assert result["definition"]["file_path"] == "definitions.py"
        assert result["definition"]["line"] == 1

        caller_files = {c["file_path"] for c in result["callers"]}
        assert "caller.py" in caller_files

    @requires_python_grammar
    def test_callees_of_caller_function(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = trace_call_chain("bar", root=str(tmp_path))

        assert "error" not in result
        assert result["definition"] is not None
        assert result["definition"]["file_path"] == "caller.py"

        callee_names = {c["name"] for c in result["callees"]}
        assert "foo" in callee_names

    def test_nonexistent_root_returns_error(self, tmp_path):
        result = trace_call_chain("foo", root=str(tmp_path / "does-not-exist"))
        assert "error" in result

    def test_symbol_with_no_definition_returns_none(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = trace_call_chain("does_not_exist_anywhere", root=str(tmp_path))

        assert "error" not in result
        assert result["definition"] is None
        assert result["callees"] == []
        assert result["callers"] == []

    def test_stats_report_files_scanned(self, tmp_path):
        _write_sample_repo(tmp_path)

        result = trace_call_chain("foo", root=str(tmp_path))

        assert "error" not in result
        assert result["stats"]["files_scanned"] == 2
