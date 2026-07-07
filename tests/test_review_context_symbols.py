"""
Tests for the fixed changed-symbol selection in mr_review_context.

The old heuristic ranked identifiers by frequency across add+context lines, so
common tokens (`the`, `number`, `styles`) won. The fix keys off real changed
declarations via the same AST/section resolution as mr_diff_sections, with an
added-line identifier fallback only when nothing resolves.
"""

from __future__ import annotations

import myopic.tools.review_context as rc
from myopic.platforms.base import DiffSet, FileDiff, ReviewMetadata
from myopic.tools.diff_sections import changed_symbols


class _FakeReview:
    def __init__(self, files):
        self._files = files

    def metadata(self):
        return ReviewMetadata(1, "t", "a", "s", "m")

    def diffs(self):
        return DiffSet(files=self._files)


def _setup(monkeypatch, files):
    monkeypatch.setattr(rc, "open_review", lambda url: _FakeReview(files))
    monkeypatch.setattr(rc, "dependency_impact",
                        lambda sym, root: {"symbol": sym, "total_references": 0})


# --- changed_symbols (the shared extractor) ---------------------------------

def test_changed_symbols_new_file_returns_real_defs():
    patch = (
        "@@ -0,0 +1,6 @@\n"
        "+def process_payment(amount):\n"
        "+    the = amount\n"
        "+    number = the + 1\n"
        "+    return number\n"
        "+\n"
        "+def refund(txn):\n"
        "+    return txn\n"
    )
    names = [c["symbol"] for c in changed_symbols(patch, "python", is_new_file=True)]
    assert "process_payment" in names and "refund" in names
    assert "the" not in names and "number" not in names and "amount" not in names


def test_changed_symbols_none_for_data_file():
    patch = '@@ -1,2 +1,3 @@\n {\n+  "apiEndpoint": "x",\n   "timeout": 30\n }\n'
    assert changed_symbols(patch, None, is_new_file=False) == []


# --- mr_review_context end to end -------------------------------------------

def test_ast_symbols_selected_not_stopwords(monkeypatch, tmp_path):
    patch = (
        "@@ -0,0 +1,6 @@\n"
        "+def process_payment(amount):\n"
        "+    the = amount\n"
        "+    number = the + 1\n"
        "+    return number\n"
        "+\n"
        "+def refund(txn):\n"
        "+    return txn\n"
    )
    _setup(monkeypatch, [FileDiff("pay.py", "pay.py", new_file=True, patch=patch)])
    out = rc.mr_review_context("u", str(tmp_path))
    syms = [s["symbol"] for s in out["symbols"]]
    assert out["symbol_source"] == "ast"
    assert "process_payment" in syms and "refund" in syms
    assert not ({"the", "number", "amount"} & set(syms))
    # symbol_type carried through
    assert all("symbol_type" in s for s in out["symbols"])


def test_modified_file_uses_hunk_header_symbol(monkeypatch, tmp_path):
    patch = (
        "@@ -10,3 +10,4 @@ def calculate_total(items):\n"
        "     total = 0\n"
        "-    for i in items:\n"
        "+    for item in items:\n"
        "+        total += item.price\n"
        "     return total\n"
    )
    _setup(monkeypatch, [FileDiff("calc.py", "calc.py", patch=patch)])
    out = rc.mr_review_context("u", str(tmp_path))
    assert out["symbol_source"] == "ast"
    assert "calculate_total" in [s["symbol"] for s in out["symbols"]]


def test_identifier_fallback_when_no_declarations(monkeypatch, tmp_path):
    patch = '@@ -1,2 +1,3 @@\n {\n+  "apiEndpoint": "https",\n   "timeout": 30\n }\n'
    _setup(monkeypatch, [FileDiff("config.json", "config.json", patch=patch)])
    out = rc.mr_review_context("u", str(tmp_path))
    assert out["symbol_source"] == "identifier-fallback"
    assert any(s["symbol"] == "apiEndpoint" for s in out["symbols"])


def test_max_symbols_cap(monkeypatch, tmp_path):
    body = "".join(f"+def fn_{i}():\n+    return {i}\n" for i in range(20))
    patch = f"@@ -0,0 +1,40 @@\n{body}"
    _setup(monkeypatch, [FileDiff("many.py", "many.py", new_file=True, patch=patch)])
    out = rc.mr_review_context("u", str(tmp_path), max_symbols=5)
    assert len(out["symbols"]) == 5
