"""Unit tests for the platform-agnostic diff parser. No network required."""

from myopic.diff import count_lines, find_line_mappings, parse_hunks

SAMPLE_PATCH = """\
@@ -1,4 +1,5 @@
 def greet(name):
-    return "hi " + name
+    if not name:
+        raise ValueError("name required")
+    return f"hi {name}"
 # end
"""


class TestCountLines:
    def test_counts_adds_and_dels(self):
        adds, dels = count_lines(SAMPLE_PATCH)
        assert adds == 3
        assert dels == 1

    def test_ignores_file_headers(self):
        patch = "+++ b/file.py\n--- a/file.py\n+real add\n-real del\n"
        adds, dels = count_lines(patch)
        assert adds == 1
        assert dels == 1

    def test_empty_patch(self):
        assert count_lines("") == (0, 0)


class TestParseHunks:
    def test_single_hunk_structure(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        assert len(hunks) == 1
        h = hunks[0]
        assert h["old_start"] == 1
        assert h["new_start"] == 1

    def test_line_types_and_numbers(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        lines = hunks[0]["lines"]
        types = [l["type"] for l in lines]
        assert types == ["context", "del", "add", "add", "add", "context"]

        # The deletion has an old_line but no new_line.
        deletion = next(l for l in lines if l["type"] == "del")
        assert deletion["old_line"] == 2
        assert deletion["new_line"] is None

        # Additions have new_line but no old_line, numbered consecutively.
        adds = [l for l in lines if l["type"] == "add"]
        assert [l["new_line"] for l in adds] == [2, 3, 4]
        assert all(l["old_line"] is None for l in adds)

    def test_content_strips_prefix(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        first_add = next(l for l in hunks[0]["lines"] if l["type"] == "add")
        assert first_add["content"] == '    if not name:'

    def test_no_newline_marker_skipped(self):
        patch = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"
        hunks = parse_hunks(patch)
        contents = [l["content"] for l in hunks[0]["lines"]]
        assert "" not in [c for c in contents if c.startswith("\\")]
        assert len(hunks[0]["lines"]) == 2

    def test_multiple_hunks(self):
        patch = (
            "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
            "@@ -10,2 +10,2 @@\n x\n-y\n+Y\n"
        )
        hunks = parse_hunks(patch)
        assert len(hunks) == 2
        assert hunks[1]["new_start"] == 10

    def test_lines_before_any_hunk_ignored(self):
        patch = "diff --git a/f b/f\nindex abc..def\n@@ -1 +1 @@\n-a\n+b\n"
        hunks = parse_hunks(patch)
        assert len(hunks) == 1
        assert len(hunks[0]["lines"]) == 2


class TestFindLineMappings:
    def test_exact_match(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        mappings = find_line_mappings(hunks, [3])
        assert len(mappings) == 1
        m = mappings[0]
        assert m["requested"] == 3
        assert m["new_line"] == 3
        assert m["exact"] is True

    def test_nearest_fallback(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        # Line 99 doesn't exist; should snap to the closest new-side line.
        mappings = find_line_mappings(hunks, [99])
        assert mappings[0]["exact"] is False
        assert mappings[0]["new_line"] is not None

    def test_deduplicates_and_sorts(self):
        hunks = parse_hunks(SAMPLE_PATCH)
        mappings = find_line_mappings(hunks, [4, 2, 2])
        requested = [m["requested"] for m in mappings]
        assert requested == [2, 4]

    def test_empty_hunks_returns_null_mapping(self):
        mappings = find_line_mappings([], [5])
        assert mappings[0]["new_line"] is None
        assert mappings[0]["exact"] is False
