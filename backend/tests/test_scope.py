"""
Request classification: mode (answer/edit/command) and minimal scope.
"""
import os

import pytest

from app.agent.context import (classify_request, detect_mode, invalidate_workspace_files,
                               resolve_explicit_files, build_context)


@pytest.fixture
def repo(tmp_path):
    files = {
        "demo.py": "print('demo')\n",
        "app/auth/routes.py": "def login(): pass\n",
        "app/auth/utils.py": "def token(): pass\n",
        "app/api/chat.py": "def chat(): pass\n",
        "app/api/__init__.py": "",
        "app/models/__init__.py": "",
        "README.md": "# Demo project\n",
        "Dockerfile": "FROM python\n",
        ".env": "SECRET=1\n",
    }
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    invalidate_workspace_files(str(tmp_path))
    return str(tmp_path)


@pytest.mark.parametrize("msg,mode", [
    ("Explain demo.py", "answer"),
    ("What does demo.py do?", "answer"),
    ("How do I add a function to a class in python?", "answer"),
    ("Can you explain how to add tests?", "answer"),
    ("Add a comment at the top of demo.py explaining its purpose", "edit"),
    ("Inspect demo.py and add a comment at the top explaining the purpose of the file", "edit"),
    ("Please add logging to app/api/chat.py", "edit"),
    ("Can you rename login to sign_in in routes.py?", "edit"),
    ("For demo.py, replace print with logging", "edit"),
    ("Review auth.py and fix any bugs", "edit"),
    ("Find the authentication flow and refactor it to use sessions", "edit"),
    ("in requirment file add one more requirment that is python nothing else", "edit"),
    ("Make the button blue", "edit"),
    ("run the tests", "command"),
    ("pytest -q tests/test_api.py", "command"),
    ("How does authentication work?", "answer"),
    ("Write a very detailed explanation of every function in app/auth.py", "answer"),
    ("Write a very detailed, long explanation of every function in app/auth.py", "answer"),
    ("Give me a summary of the changes needed to fix auth.py", "answer"),
    ("Write a function that validates emails in utils.py", "edit"),
    ("Write a README for this project", "edit"),
])
def test_detect_mode(msg, mode):
    assert detect_mode(msg) == mode


def test_explicit_file_edit_is_narrow_scope(repo):
    plan = classify_request("Add a comment at the top of demo.py explaining its purpose", repo)
    assert (plan.mode, plan.scope, plan.explicit_files) == ("edit", "explicit_file", ["demo.py"])


def test_explain_file_is_answer_on_explicit_file(repo):
    plan = classify_request("Explain demo.py", repo)
    assert (plan.mode, plan.scope) == ("answer", "explicit_file")


def test_multiple_files(repo):
    plan = classify_request("Compare app/auth/routes.py and chat.py", repo)
    assert plan.scope == "explicit_files"
    assert plan.explicit_files == ["app/auth/routes.py", "app/api/chat.py"]


def test_repo_question_without_filename_uses_repository_scope(repo):
    plan = classify_request("How does authentication work?", repo)
    assert plan.scope == "repository" and plan.mode == "answer"


def test_project_overview(repo):
    plan = classify_request("Explain this project", repo)
    assert plan.scope == "repository" and plan.overview


def test_general_question_stays_general(repo):
    assert classify_request("What is a closure in python?", repo).scope == "general"
    assert classify_request("What's the difference between a list and a tuple?", repo).scope == "general"


def test_edit_without_file_searches_repository(repo):
    plan = classify_request("Find the authentication flow and refactor it to use sessions", repo)
    assert (plan.mode, plan.scope) == ("edit", "repository")


def test_no_workspace_is_general(repo):
    assert classify_request("Explain demo.py", "").scope == "general"


def test_secret_files_are_never_explicit_targets(repo):
    files, _ = resolve_explicit_files("show me .env", repo)
    assert files == []


def test_ambiguous_basename_is_reported(repo):
    files, notes = resolve_explicit_files("open __init__.py", repo)
    assert files == ["app/api/__init__.py"]
    assert notes and "matches 2 files" in notes[0]


def test_extensionless_and_nonexistent(repo):
    assert resolve_explicit_files("Explain the Dockerfile", repo)[0] == ["Dockerfile"]
    assert resolve_explicit_files("Explain missing.py e.g. v1.2", repo)[0] == []


def test_history_merges_consecutive_roles():
    msgs = [{"role": "user", "content": "q1"}, {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "q3"}]
    out = build_context(msgs, "sys")
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == "q1\n\nq2"


def test_named_file_plus_codebase_search_is_repository_with_pins(repo):
    plan = classify_request("Find the authentication flow and refactor it, and make chat.py use the helper", repo)
    assert plan.scope == "repository" and plan.explicit_files == ["app/api/chat.py"]
    # a plain named-file edit stays narrow
    assert classify_request("Add logging to chat.py", repo).scope == "explicit_file"
