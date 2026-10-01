"""
End-to-end workflow tests: HTTP API → agent runner → provider → persistence.

The LLM is replaced by ScriptedProvider (deterministic responses, records every
prompt it receives, can block until cancelled). Everything else — scope
detection, context gathering, search, edit validation, diffing, apply/revert,
SSE streaming, cancellation, persistence — is the production code path.
"""
import json
import os
import threading
import time

import pytest

from app.database import db
from app.llm import factory as factory_mod
from app.llm.base import LLMProvider, ModelInfo, ProviderCancelled, ProviderError, Usage
from app.models.user import User
from app.auth.utils import generate_token

DEMO = '''import sys


def main():
    print("hello from demo")


if __name__ == "__main__":
    main()
'''

AUTH = '''from flask import request


def login(username, password):
    token = issue_token(username)
    return token


def issue_token(username):
    return "tok-" + username
'''

ROUTES = '''from auth import login


def login_route():
    return login(request.form["u"], request.form["p"])
'''


class ScriptedProvider(LLMProvider):
    """Returns queued responses; 'BLOCK' waits for cancellation; exceptions are raised."""
    label = "Scripted"

    def __init__(self, script):
        super().__init__()
        self.script = script
        self.calls = []
        self._model = "scripted-1"

    @property
    def provider_name(self):
        return "openai"

    def get_model_info(self):
        return ModelInfo("openai", self._model, "Scripted", 100_000, 8_000)

    def stream(self, messages, system_prompt="", cancel_event=None, json_mode=False, max_tokens=None, **kw):
        self.calls.append({"messages": messages, "system": system_prompt, "json_mode": json_mode})
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        if item == "BLOCK":
            yield "partial answer "
            while not cancel_event.wait(0.05):
                pass
            raise ProviderCancelled("openai", self._model)
        for i in range(0, len(item), 40):
            yield item[i:i + 40]
        self.last_usage = Usage(len(system_prompt) // 4 + 10, len(item) // 4, False)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "demo.py").write_text(DEMO)
    (root / "app" / "auth.py").write_text(AUTH)
    (root / "app" / "routes.py").write_text(ROUTES)
    (root / "README.md").write_text("# Demo project\nA tiny app with login.\n")
    return str(root)


@pytest.fixture
def scripted(monkeypatch):
    holder = {}

    def install(*responses):
        p = ScriptedProvider(list(responses))
        holder["p"] = p
        monkeypatch.setattr(factory_mod.ProviderFactory, "get", staticmethod(lambda *a, **k: p))
        return p
    return install


@pytest.fixture
def workspace(client, auth_headers, repo):
    res = client.post("/api/workspaces", headers=auth_headers, json={"name": "proj", "repo_path": repo})
    assert res.status_code == 201, res.get_json()
    ws = res.get_json()
    # wait for the background index so repository searches are deterministic
    for _ in range(100):
        st = client.get(f"/api/workspaces/{ws['id']}/index/status", headers=auth_headers).get_json()
        if st.get("status") == "done":
            break
        time.sleep(0.05)
    return ws


def run_chat(client, headers, **body):
    body.setdefault("provider", "openai")
    res = client.post("/api/chat", headers=headers, json=body, buffered=False)
    assert res.status_code == 200, res.get_data(as_text=True)
    events = []
    buf = ""
    for chunk in res.response:
        buf += chunk.decode() if isinstance(chunk, bytes) else chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            if block.startswith("data: "):
                events.append(json.loads(block[6:]))
    return events


def of(events, t):
    return [e for e in events if e["type"] == t]


def activity_texts(events):
    acts = of(events, "activity")
    return [i["text"] for i in acts[-1]["items"]] if acts else []


def edit_json(files, summary="Change"):
    return json.dumps({"summary": summary, "plan": [], "files": files})


# ── Scenario 1: project explanation ──────────────────────────────────────────

def test_explain_project_uses_overview_and_streams(client, auth_headers, workspace, scripted):
    p = scripted("This project is a tiny login app.")
    ev = run_chat(client, auth_headers, message="Explain this project", workspace_id=workspace["id"])
    assert of(ev, "plan")[0]["scope"] == "repository"
    assert "".join(e["content"] for e in of(ev, "chunk")) == "This project is a tiny login app."
    assert of(ev, "done")
    system = p.calls[0]["system"]
    assert "Project file tree" in system and "README.md" in system
    assert any("project structure" in t for t in activity_texts(ev))


# ── Scenario 2: explicit file explanation — no repository search ────────────

def test_explain_explicit_file_reads_only_that_file(client, auth_headers, workspace, scripted, monkeypatch):
    from app.repository import searcher
    monkeypatch.setattr(searcher, "search", lambda *a, **k: pytest.fail("repository search must not run"))
    p = scripted("demo.py prints a greeting.")
    ev = run_chat(client, auth_headers, message="Explain demo.py", workspace_id=workspace["id"])
    plan = of(ev, "plan")[0]
    assert (plan["mode"], plan["scope"], plan["explicit_files"]) == ("answer", "explicit_file", ["demo.py"])
    system = p.calls[0]["system"]
    assert 'print("hello from demo")' in system
    assert "auth.py" not in system and "README" not in system  # minimal context
    assert not of(ev, "proposal")
    assert any(t.startswith("Read demo.py") for t in activity_texts(ev))


# ── Scenario 3: explicit edit → diff → accept → apply → verify ──────────────

def test_explicit_edit_accept_flow(client, auth_headers, workspace, scripted, repo):
    p = scripted(edit_json([{"path": "demo.py", "action": "modify", "operations": [
        {"type": "prepend", "content": '"""Demo entry point: prints a greeting."""\n'}]}],
        "Added a module docstring describing the file's purpose."))
    ev = run_chat(client, auth_headers, message="Add a comment at the top of demo.py explaining its purpose",
                  workspace_id=workspace["id"])
    assert p.calls[0]["json_mode"] is True
    assert "1| import sys" in p.calls[0]["system"]  # numbered listing for precise ops
    prop = of(ev, "proposal")[0]["change"]
    assert prop["status"] == "pending"
    diff = prop["files"][0]["diff"]
    assert '+"""Demo entry point: prints a greeting."""' in diff
    assert prop["files"][0]["additions"] == 1 and prop["files"][0]["deletions"] == 0
    # nothing written before approval
    assert open(os.path.join(repo, "demo.py")).read() == DEMO

    res = client.post(f"/api/changes/{prop['id']}/apply", headers=auth_headers)
    assert res.status_code == 200, res.get_json()
    body = res.get_json()["change"]
    assert body["status"] == "applied"
    assert body["verification"][0]["ok"] is True
    assert open(os.path.join(repo, "demo.py")).read() == '"""Demo entry point: prints a greeting."""\n' + DEMO

    # the proposal survives reload via the conversation payload
    conv = client.get(f"/api/conversations/{of(ev, 'done')[0]['conversation_id']}", headers=auth_headers).get_json()
    assert conv["messages"][-1]["change"]["status"] == "applied"


# ── Scenario 4: reject leaves the workspace untouched ───────────────────────

def test_reject_leaves_file_unchanged(client, auth_headers, workspace, scripted, repo):
    scripted(edit_json([{"path": "demo.py", "operations": [{"type": "prepend", "content": "# x\n"}]}]))
    ev = run_chat(client, auth_headers, message="Add a comment to demo.py", workspace_id=workspace["id"])
    cid = of(ev, "proposal")[0]["change"]["id"]
    res = client.post(f"/api/changes/{cid}/reject", headers=auth_headers)
    assert res.get_json()["change"]["status"] == "rejected"
    assert open(os.path.join(repo, "demo.py")).read() == DEMO
    assert client.post(f"/api/changes/{cid}/apply", headers=auth_headers).status_code == 409


def test_apply_refuses_when_user_edited_file_after_proposal(client, auth_headers, workspace, scripted, repo):
    scripted(edit_json([{"path": "demo.py", "operations": [{"type": "prepend", "content": "# x\n"}]}]))
    ev = run_chat(client, auth_headers, message="Add a comment to demo.py", workspace_id=workspace["id"])
    cid = of(ev, "proposal")[0]["change"]["id"]
    user_version = DEMO + "# my own edit\n"
    with open(os.path.join(repo, "demo.py"), "w") as f:
        f.write(user_version)
    res = client.post(f"/api/changes/{cid}/apply", headers=auth_headers)
    assert res.status_code == 409 and res.get_json()["kind"] == "stale"
    assert open(os.path.join(repo, "demo.py")).read() == user_version


def test_revert_restores_original(client, auth_headers, workspace, scripted, repo):
    scripted(edit_json([{"path": "demo.py", "operations": [
        {"type": "replace", "old_text": 'print("hello from demo")', "new_text": 'print("hi")'}]}]))
    ev = run_chat(client, auth_headers, message="Change the greeting in demo.py", workspace_id=workspace["id"])
    cid = of(ev, "proposal")[0]["change"]["id"]
    assert client.post(f"/api/changes/{cid}/apply", headers=auth_headers).status_code == 200
    assert 'print("hi")' in open(os.path.join(repo, "demo.py")).read()
    res = client.post(f"/api/changes/{cid}/revert", headers=auth_headers)
    assert res.status_code == 200 and res.get_json()["change"]["status"] == "reverted"
    assert open(os.path.join(repo, "demo.py")).read() == DEMO


def test_invalid_edit_is_repaired_once(client, auth_headers, workspace, scripted):
    bad = edit_json([{"path": "demo.py", "operations": [
        {"type": "replace", "old_text": "print('nope')", "new_text": "x"}]}])
    good = edit_json([{"path": "demo.py", "operations": [{"type": "prepend", "content": "# ok\n"}]}])
    p = scripted(bad, good)
    ev = run_chat(client, auth_headers, message="Add a comment to demo.py", workspace_id=workspace["id"])
    assert len(p.calls) == 2
    assert "was not found" in p.calls[1]["messages"][-1]["content"]  # error fed back to the model
    assert of(ev, "proposal")
    assert any("requesting a fix" in t for t in activity_texts(ev))


def test_invalid_edit_twice_fails_cleanly_and_conversation_continues(client, auth_headers, workspace, scripted):
    scripted("I added the comment.", "Still prose, not JSON.", "Here is the answer.")
    ev = run_chat(client, auth_headers, message="Add a comment to demo.py", workspace_id=workspace["id"])
    err = of(ev, "error")[0]
    assert err["kind"] == "edit_invalid" and "not valid JSON" in err["message"]
    conv_id = of(ev, "start")[0]["conversation_id"]
    ev2 = run_chat(client, auth_headers, message="Explain demo.py", conversation_id=conv_id,
                   workspace_id=workspace["id"])
    assert of(ev2, "done")
    msgs = client.get(f"/api/conversations/{conv_id}", headers=auth_headers).get_json()["messages"]
    assert [m["status"] for m in msgs if m["role"] == "assistant"] == ["error", "complete"]


def test_retry_replaces_failed_turn(client, auth_headers, workspace, scripted):
    scripted(ProviderError("openai", "timeout", "Scripted request timed out after 90 seconds"), "Recovered.")
    ev = run_chat(client, auth_headers, message="Explain demo.py", workspace_id=workspace["id"])
    err = of(ev, "error")[0]
    assert err["retryable"] is True and "timed out" in err["message"]
    conv_id = of(ev, "start")[0]["conversation_id"]
    ev2 = run_chat(client, auth_headers, message="Explain demo.py", conversation_id=conv_id,
                   workspace_id=workspace["id"], retry=True)
    assert of(ev2, "done")
    msgs = client.get(f"/api/conversations/{conv_id}", headers=auth_headers).get_json()["messages"]
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "complete"), ("assistant", "complete")]


# ── Scenario 5: stop cancels the provider request; next request works ───────

def test_cancel_stops_run_and_next_request_works(app, client, auth_headers, workspace, scripted):
    p = scripted("BLOCK", "Second answer.")
    req_id = "req-cancel-test"
    result = {}

    def stop_soon():
        time.sleep(0.6)
        with app.test_client() as c2:
            result["cancel"] = c2.post("/api/chat/cancel", headers=auth_headers, json={"request_id": req_id})

    threading.Thread(target=stop_soon).start()
    t0 = time.monotonic()
    ev = run_chat(client, auth_headers, message="Explain demo.py", workspace_id=workspace["id"], request_id=req_id)
    elapsed = time.monotonic() - t0
    assert of(ev, "cancelled"), [e["type"] for e in ev]
    assert elapsed < 4
    assert result["cancel"].status_code == 200
    conv_id = of(ev, "start")[0]["conversation_id"]
    msgs = client.get(f"/api/conversations/{conv_id}", headers=auth_headers).get_json()["messages"]
    assert msgs[-1]["status"] == "cancelled" and "partial answer" in msgs[-1]["content"]
    ev2 = run_chat(client, auth_headers, message="What does main() do in demo.py?", conversation_id=conv_id,
                   workspace_id=workspace["id"])
    assert "".join(e["content"] for e in of(ev2, "chunk")) == "Second answer."
    # the abandoned question is not carried into the next prompt
    assert p.calls[1]["messages"] == [{"role": "user", "content": "What does main() do in demo.py?"}]


# ── Scenario 6: repository question searches the index ──────────────────────

def test_repository_question_searches(client, auth_headers, workspace, scripted):
    p = scripted("Login issues a token via issue_token().")
    ev = run_chat(client, auth_headers, message="How does authentication work?", workspace_id=workspace["id"])
    assert of(ev, "plan")[0]["scope"] == "repository"
    texts = activity_texts(ev)
    assert any(t.startswith("Found") and "relevant sections" in t for t in texts), texts
    assert "def issue_token" in p.calls[0]["system"]


# ── Scenario 7: multi-file refactor ─────────────────────────────────────────

def test_multi_file_refactor(client, auth_headers, workspace, scripted, repo):
    p = scripted(edit_json([
        {"path": "app/auth.py", "operations": [
            {"type": "replace", "old_text": 'return "tok-" + username',
             "new_text": 'return session_token(username)'},
            {"type": "append", "content": '\n\ndef session_token(username):\n    return "sess-" + username\n'}]},
        {"path": "app/routes.py", "operations": [
            {"type": "replace", "old_text": "from auth import login", "new_text": "from app.auth import login"}]},
    ], "Switched token issuing to sessions."))
    ev = run_chat(client, auth_headers, message="Find the authentication flow and refactor it to use sessions",
                  workspace_id=workspace["id"])
    plan = of(ev, "plan")[0]
    assert (plan["mode"], plan["scope"]) == ("edit", "repository")
    assert "def issue_token" in p.calls[0]["system"]
    change = of(ev, "proposal")[0]["change"]
    assert [f["path"] for f in change["files"]] == ["app/auth.py", "app/routes.py"]
    assert client.post(f"/api/changes/{change['id']}/apply", headers=auth_headers).status_code == 200
    assert "sess-" in open(os.path.join(repo, "app", "auth.py")).read()
    assert "from app.auth import login" in open(os.path.join(repo, "app", "routes.py")).read()


# ── Providers ────────────────────────────────────────────────────────────────

def test_provider_error_is_specific_and_marks_provider(client, auth_headers, workspace, scripted, app):
    scripted(ProviderError("openai", "quota", "OpenAI quota exhausted: You have no credits remaining"))
    ev = run_chat(client, auth_headers, message="Explain demo.py", workspace_id=workspace["id"])
    err = of(ev, "error")[0]
    assert err["kind"] == "provider_quota" and "no credits" in err["message"]
    assert err["retryable"] is False
    with app.app_context():
        info = factory_mod.ProviderFactory.get_all_info(app.config, live_models=False)
    # quota is per model: the model is flagged, the provider stays usable
    assert info["openai"]["status"] == "ready"
    assert "no credits" in info["openai"]["model_errors"]["scripted-1"]
    factory_mod.record_provider_result("openai", None, "scripted-1")


def test_unconfigured_provider_rejected_before_streaming(client, auth_headers):
    res = client.post("/api/chat", headers=auth_headers, json={"message": "hi", "provider": "anthropic"})
    assert res.status_code == 400
    assert "not configured" in res.get_json()["error"]


def test_ollama_disabled_does_not_block_cloud(client, auth_headers, scripted):
    res = client.post("/api/chat", headers=auth_headers, json={"message": "hi", "provider": "ollama"})
    assert res.status_code == 400 and "Ollama is disabled" in res.get_json()["error"]
    scripted("Hello!")
    ev = run_chat(client, auth_headers, message="What is a closure?")
    assert of(ev, "done")


# ── Commands require permission ──────────────────────────────────────────────

def test_command_requires_approval_then_runs(client, auth_headers, workspace, scripted):
    scripted(json.dumps({"command": "python -c print(42)", "reason": "prints a number"}))
    ev = run_chat(client, auth_headers, message="run python -c print(42)", workspace_id=workspace["id"])
    cmd = of(ev, "command")[0]["command"]
    assert cmd["status"] == "pending" and cmd["command"] == "python -c print(42)"
    res = client.post(f"/api/commands/{cmd['id']}/approve", headers=auth_headers)
    out = res.get_json()["command"]
    assert out["status"] == "completed" and "42" in out["result"]["output"]


def test_destructive_command_is_blocked(client, auth_headers, workspace, scripted):
    scripted(json.dumps({"command": "rm -rf /", "reason": "cleanup"}))
    ev = run_chat(client, auth_headers, message="run a cleanup", workspace_id=workspace["id"])
    assert not of(ev, "command")
    assert any("blocked by safety policy" in t for t in activity_texts(ev))


# ── Multi-user isolation ─────────────────────────────────────────────────────

def test_users_cannot_touch_each_others_work(app, client, auth_headers, workspace, scripted, repo):
    scripted(edit_json([{"path": "demo.py", "operations": [{"type": "prepend", "content": "# x\n"}]}]))
    ev = run_chat(client, auth_headers, message="Add a comment to demo.py", workspace_id=workspace["id"])
    cid = of(ev, "proposal")[0]["change"]["id"]
    conv_id = of(ev, "start")[0]["conversation_id"]
    with app.app_context():
        other = User(email="other@example.com", name="Other")
        other.set_password("anotherpassword1")
        db.session.add(other)
        db.session.commit()
        h2 = {"Authorization": f"Bearer {generate_token(other.id)}"}
    assert client.get(f"/api/changes/{cid}", headers=h2).status_code == 404
    assert client.post(f"/api/changes/{cid}/apply", headers=h2).status_code == 404
    assert client.get(f"/api/conversations/{conv_id}", headers=h2).status_code == 404
    assert client.get(f"/api/workspaces/{workspace['id']}/files", headers=h2).status_code == 404
    res = client.post("/api/chat", headers=h2, json={"message": "Explain demo.py", "provider": "openai",
                                                      "workspace_id": workspace["id"]})
    assert res.status_code == 404
    assert open(os.path.join(repo, "demo.py")).read() == DEMO


def test_path_policy_blocks_escape_and_secrets(client, auth_headers, workspace, scripted, repo):
    scripted(edit_json([{"path": "../outside.py", "action": "create", "content": "x = 1"}]),
             edit_json([{"path": ".env", "action": "create", "content": "SECRET=1"}]))
    ev = run_chat(client, auth_headers, message="Add a new helper file", workspace_id=workspace["id"])
    assert of(ev, "error")[0]["kind"] == "edit_invalid"
    assert not os.path.exists(os.path.join(os.path.dirname(repo), "outside.py"))
    assert not os.path.exists(os.path.join(repo, ".env"))
