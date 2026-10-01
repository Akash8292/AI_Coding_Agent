"""
Structured edit engine: parsing, deterministic application, validation, diffs.
"""
import pytest

from app.agent.editor import (
    EditError, apply_operations, build_changes, extract_json, parse_edit_plan,
    unified_diff, diff_stats, syntax_error,
)

SAMPLE = """def add(a, b):
    return a + b

def main():
    print(add(1, 2))
"""


def ops(*o):
    return list(o)


# ── apply_operations ──────────────────────────────────────────────────────────

def test_prepend_comment_preserves_everything_else():
    out = apply_operations(SAMPLE, ops({"type": "prepend", "content": "# Math helpers"}), "m.py")
    assert out == "# Math helpers\n" + SAMPLE


def test_insert_before_line_and_after_line_are_equivalent():
    a = apply_operations(SAMPLE, ops({"type": "insert", "line": 4, "content": "# entry\n"}))
    b = apply_operations(SAMPLE, ops({"type": "insert", "after_line": 3, "content": "# entry\n"}))
    assert a == b
    assert a.splitlines()[3] == "# entry"


def test_append_to_file_without_trailing_newline():
    out = apply_operations("x = 1", ops({"type": "append", "content": "y = 2\n"}))
    assert out == "x = 1\ny = 2\n"


def test_replace_exact_unique():
    out = apply_operations(SAMPLE, ops({"type": "replace", "old_text": "return a + b",
                                        "new_text": "return int(a) + int(b)"}))
    assert "return int(a) + int(b)" in out and "return a + b" not in out


def test_replace_ambiguous_is_rejected():
    text = "x = 1\nx = 1\n"
    with pytest.raises(EditError, match="matches 2 places"):
        apply_operations(text, ops({"type": "replace", "old_text": "x = 1", "new_text": "x = 2"}))


def test_replace_missing_text_is_rejected_not_ignored():
    with pytest.raises(EditError, match="not found"):
        apply_operations(SAMPLE, ops({"type": "replace", "old_text": "return a - b", "new_text": "x"}))


def test_replace_tolerates_indentation_and_trailing_whitespace_when_unique():
    out = apply_operations(SAMPLE, ops({"type": "replace", "old_text": "return a + b   ",
                                        "new_text": "    return a + b + 0"}))
    assert "    return a + b + 0\n" in out


def test_multiple_ops_use_original_coordinates():
    # line numbers refer to the ORIGINAL file even after an earlier insert
    out = apply_operations(SAMPLE, ops(
        {"type": "prepend", "content": "# header\n"},
        {"type": "replace_lines", "start_line": 4, "end_line": 5,
         "content": "def main():\n    print(add(2, 3))\n"},
    ))
    assert out.startswith("# header\ndef add")
    assert "print(add(2, 3))" in out and "print(add(1, 2))" not in out


def test_overlapping_ops_rejected():
    with pytest.raises(EditError, match="overlap"):
        apply_operations(SAMPLE, ops(
            {"type": "replace_lines", "start_line": 1, "end_line": 2, "content": "a\n"},
            {"type": "delete", "start_line": 2, "end_line": 3},
        ))


def test_line_out_of_range_rejected():
    with pytest.raises(EditError, match="valid: 1..6"):
        apply_operations(SAMPLE, ops({"type": "insert", "line": 40, "content": "x"}))


def test_delete_text_removes_whole_line():
    out = apply_operations(SAMPLE, ops({"type": "delete", "old_text": "    print(add(1, 2))"}))
    assert out == "def add(a, b):\n    return a + b\n\ndef main():\n"


def test_unknown_op_type_rejected():
    with pytest.raises(EditError, match="unknown type"):
        apply_operations(SAMPLE, ops({"type": "teleport"}))


def test_crlf_input_is_normalised():
    out = apply_operations("a\r\nb\r\n", ops({"type": "insert", "line": 2, "content": "x"}))
    assert out == "a\nx\nb\n"


# ── parsing ──────────────────────────────────────────────────────────────────

def test_extract_json_bare_fenced_and_with_prose():
    obj = {"summary": "s", "files": []}
    assert extract_json('{"summary": "s", "files": []}') == obj
    assert extract_json('```json\n{"summary": "s", "files": []}\n```') == obj
    assert extract_json('Sure! Here it is:\n{"summary": "s", "files": []}\nDone.') == obj


def test_extract_json_rejects_prose():
    with pytest.raises(EditError, match="not valid JSON"):
        extract_json("I added a comment to the top of the file.")


def test_parse_plan_single_file_shorthand_and_aliases():
    plan = parse_edit_plan({"explanation": "x", "file": "a.py",
                            "changes": [{"type": "insert_after", "line": 0, "content": "# hi"}]})
    assert plan.files[0].path == "a.py"
    assert plan.files[0].operations[0] == {"type": "insert", "after_line": 0, "content": "# hi"}


def test_parse_plan_requires_files():
    with pytest.raises(EditError, match='no "files"'):
        parse_edit_plan({"summary": "nothing"})


def test_duplicate_file_entries_rejected():
    with pytest.raises(EditError, match="more than once"):
        parse_edit_plan({"files": [{"path": "a.py", "operations": [{"type": "append", "content": "x"}]},
                                   {"path": "a.py", "operations": [{"type": "append", "content": "y"}]}]})


# ── build_changes ────────────────────────────────────────────────────────────

def _fs(files: dict):
    def read(p):
        if p not in files:
            raise FileNotFoundError(p)
        return files[p]
    return read, (lambda p: p in files)


def test_build_changes_multi_file_with_create():
    read, exists = _fs({"app.py": SAMPLE, "util.py": "X = 1\n"})
    plan = parse_edit_plan({"summary": "refactor", "files": [
        {"path": "app.py", "operations": [{"type": "prepend", "content": "from util import X\n"}]},
        {"path": "util.py", "operations": [{"type": "replace", "old_text": "X = 1", "new_text": "X = 2"}]},
        {"path": "new_mod.py", "action": "create", "content": "def f():\n    return 1"},
    ]})
    changes = build_changes(plan, read, exists)
    assert [c.path for c in changes] == ["app.py", "util.py", "new_mod.py"]
    assert changes[2].original is None and changes[2].new.endswith("\n")
    assert changes[0].diff.startswith("--- a/app.py\n+++ b/app.py")
    assert changes[2].diff.startswith("--- /dev/null")
    assert (changes[1].additions, changes[1].deletions) == (1, 1)


def test_build_changes_rejects_syntax_error_introduced_by_edit():
    read, exists = _fs({"app.py": SAMPLE})
    plan = parse_edit_plan({"files": [{"path": "app.py", "operations": [
        {"type": "replace", "old_text": "return a + b", "new_text": "return (a + b"}]}]})
    with pytest.raises(EditError, match="syntax error"):
        build_changes(plan, read, exists)


def test_build_changes_rejects_noop_and_missing_file():
    read, exists = _fs({"app.py": SAMPLE})
    with pytest.raises(EditError, match="do not change"):
        build_changes(parse_edit_plan({"files": [{"path": "app.py", "operations": [
            {"type": "replace", "old_text": "return a + b", "new_text": "return a + b"}]}]}), read, exists)
    with pytest.raises(EditError, match="does not exist"):
        build_changes(parse_edit_plan({"files": [{"path": "nope.py", "operations": [
            {"type": "append", "content": "x"}]}]}), read, exists)


def test_create_existing_file_rejected():
    read, exists = _fs({"app.py": SAMPLE})
    with pytest.raises(EditError, match="already exists"):
        build_changes(parse_edit_plan({"files": [{"path": "app.py", "action": "create", "content": "x"}]}),
                      read, exists)


def test_diff_is_computed_from_actual_contents():
    d = unified_diff("a\nb\n", "a\nB\n", "f.txt")
    assert "-b\n+B\n" in d
    assert diff_stats(d) == (1, 1)


def test_syntax_checkers():
    assert syntax_error("x.py", "def f(:\n") is not None
    assert syntax_error("x.py", "x = 1\n") is None
    assert syntax_error("x.json", "{bad") is not None
    assert syntax_error("x.txt", "{bad") is None


def test_extract_json_repairs_invalid_escapes_lexically():
    # The model wrote \' and \d inside a JSON string — invalid escapes in JSON.
    bs = "\\"
    raw = ('{"summary": "x", "files": [{"path": "a.py", "operations": '
           '[{"type": "prepend", "content": "# it' + bs + "'s " + bs + 'd ok' + bs + 'n"}]}]}')
    op = extract_json(raw)["files"][0]["operations"][0]
    assert op["content"] == "# it" + bs + "'s " + bs + "d ok\n"
