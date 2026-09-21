import os
from app.agent.context import detect_intent
from app.agent.executor import compute_diff, apply_file_edit, _is_within


def test_detect_intent():
    assert detect_intent("what is a closure in python?") == "general"
    assert detect_intent("explain how the codebase is structured") == "repository"
    assert detect_intent("add a multiplication function to the math module") == "modification"
    assert detect_intent("fix the null pointer exception") == "modification"


def test_compute_diff():
    orig = "line 1\nline 2\nline 3\n"
    prop = "line 1\nline 2 updated\nline 3\n"
    diff = compute_diff(orig, prop, "test.txt")

    assert len(diff) > 0
    kinds = [d["type"] for d in diff]
    assert "header" in kinds
    assert "hunk" in kinds
    assert "remove" in kinds
    assert "add" in kinds


def test_apply_file_edit(temp_repo):
    calc_path = "calculator.py"
    abs_calc = os.path.join(temp_repo, calc_path)

    new_content = "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    res = apply_file_edit(temp_repo, calc_path, new_content)

    assert res["ok"] is True
    assert os.path.exists(abs_calc + ".bak")

    with open(abs_calc, "r", encoding="utf-8") as f:
        assert "multiply" in f.read()


def test_path_traversal_protection(temp_repo):
    res = apply_file_edit(temp_repo, "../outside.txt", "evil content")
    assert res["ok"] is False
    assert "escapes repository boundary" in res["error"]
