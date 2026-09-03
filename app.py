"""
Local Coding Agent
-------------------
Flask backend with a chat-style frontend. Talks to a local Ollama model
or, per-request from the UI's provider dropdown, one of several cloud
APIs: Kimi (Moonshot), OpenAI, Claude (Anthropic), or Gemini (Google).
Each cloud provider needs its own API key set as an env var on the
server; the key is never sent to the browser.

Run:
    pip install -r requirements.txt
    ollama serve            # for local mode
    ollama pull codellama
    ollama pull nomic-embed-text   # for repo indexing (all providers use this)
    python app.py

Then open http://localhost:5000
"""

import difflib
import json
import os
import secrets
import subprocess
import time
import uuid

import requests
from flask import Flask, Response, render_template, request, session, stream_with_context

import repo_index
import config

app = Flask(__name__)

# Secret key signs the session cookie that keeps each browser tab's
# indexed repo + edit access scoped to that tab only. See config.py for
# why "__AUTOGEN__" (the default) is fine to leave as-is.
_configured_key = getattr(config, "SECRET_KEY", "__AUTOGEN__")
app.secret_key = _configured_key if _configured_key and _configured_key != "__AUTOGEN__" else secrets.token_hex(32)


def get_session_id():
    """Every browser tab gets its own session_id, stored in a signed
    cookie. This is what scopes repo indexing and file edits per-user —
    two people (or two tabs) never see or touch each other's data."""
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
        session.permanent = True
    return session["sid"]


def _is_within(path, root):
    """True if `path` resolves to somewhere inside `root` (or is root
    itself). Used to stop edits from escaping the indexed repo."""
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False  # e.g. different drives on Windows


def resolve_in_repo(session_id, path_str):
    """Resolve a user-supplied path (relative to the session's indexed
    repo, or absolute) to an absolute path, and verify it's actually
    inside that repo. Returns (abs_path, repo_root, error_dict_or_None)."""
    repo_root = repo_index.get_repo_root(session_id)
    if not repo_root:
        return None, None, {"error": "no repo indexed for this session — index a repo first"}

    candidate = path_str if os.path.isabs(path_str) else os.path.join(repo_root, path_str)
    abs_path = os.path.abspath(candidate)

    if not _is_within(abs_path, repo_root):
        return None, None, {"error": "path must be inside the indexed repo"}

    return abs_path, repo_root, None


def _cfg(env_name, config_attr, default=""):
    """Env var wins if set (handy for one-off overrides); otherwise use
    config.py; otherwise the given default."""
    return os.environ.get(env_name) or getattr(config, config_attr, default) or default


# ---- Config -----------------------------------------------------------
OLLAMA_HOST = _cfg("OLLAMA_HOST", "OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = _cfg("OLLAMA_MODEL", "OLLAMA_MODEL", "codellama")

CHAT_PROVIDER = _cfg("CHAT_PROVIDER", "CHAT_PROVIDER", "ollama").lower()

# Cloud providers. Model lists are curated defaults (checked Aug 2026) —
# lineups move fast, so update the model in config.py if a name has changed.
CLOUD_PROVIDERS = {
    "kimi": {
        "label": "Kimi (Moonshot)",
        "api_key": _cfg("KIMI_API_KEY", "KIMI_API_KEY"),
        "base_url": _cfg("KIMI_BASE_URL", "KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
        "default_model": _cfg("KIMI_MODEL", "KIMI_MODEL", "kimi-k2.7-code"),
        "models": ["kimi-k2.7-code", "kimi-k2.6", "kimi-k3"],
    },
    "openai": {
        "label": "OpenAI",
        "api_key": _cfg("OPENAI_API_KEY", "OPENAI_API_KEY"),
        "base_url": _cfg("OPENAI_BASE_URL", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "default_model": _cfg("OPENAI_MODEL", "OPENAI_MODEL", "gpt-5.6-terra"),
        "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "api_key": _cfg("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "base_url": _cfg("ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        "default_model": _cfg("CLAUDE_MODEL", "CLAUDE_MODEL", "claude-sonnet-5"),
        "models": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
    },
    "gemini": {
        "label": "Gemini (Google)",
        "api_key": _cfg("GEMINI_API_KEY", "GEMINI_API_KEY"),
        "base_url": _cfg("GEMINI_BASE_URL", "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        "default_model": _cfg("GEMINI_MODEL", "GEMINI_MODEL", "gemini-3.6-flash"),
        "models": ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"],
    },
}

SYSTEM_PROMPT = (
    "You are an expert coding assistant embedded in a developer's editor. "
    "You answer programming questions clearly and concisely, write correct, "
    "runnable code, and when the user pastes an incomplete snippet you "
    "complete it in the same style/language. When you show code, use "
    "fenced markdown code blocks with the language name. Prefer being "
    "direct over verbose. If the user's request is ambiguous, state the "
    "assumption you're making and proceed."
)

REPO_SYSTEM_SUFFIX = (
    "\n\nYou also have access to relevant snippets retrieved from the "
    "user's local repository, included below as context. Use them to "
    "answer accurately and reference specific files/line ranges when "
    "relevant. If the retrieved context doesn't contain what's needed to "
    "answer, say so plainly instead of guessing."
)

EDIT_SYSTEM_PROMPT = (
    "You are an expert software engineer making a precise, surgical edit "
    "to a single file. You will be given the file's full current contents "
    "and an instruction describing the change to make. "
    "Respond with ONLY the complete new contents of the file after "
    "applying the change, wrapped EXACTLY like this, with nothing before "
    "or after it — no explanation, no markdown code fences, no commentary:\n"
    "<<<FILE>>>\n"
    "...entire new file content here...\n"
    "<<<END>>>\n"
    "Preserve everything in the file that isn't related to the requested "
    "change — do not reformat, reorder, or rewrite unrelated code."
)

MAX_EDIT_FILE_BYTES = 300_000

MAX_ATTEMPTS = 3
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


# ---- Routes -------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", model=OLLAMA_MODEL)


@app.route("/api/models")
def list_models():
    """Return model options for every provider, so the frontend can offer
    a provider dropdown plus a model dropdown for whichever is selected."""
    result = {"default_provider": CHAT_PROVIDER}

    # Ollama: query the local install, excluding embedding-only models.
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        names = [
            m["name"] for m in data.get("models", [])
            if "embed" not in m["name"].lower()
        ]
        result["ollama"] = {"models": names, "current": OLLAMA_MODEL, "available": True}
    except requests.RequestException as e:
        result["ollama"] = {"models": [], "current": OLLAMA_MODEL, "available": False, "error": str(e)}

    # Cloud providers: static curated lists; "available" reflects whether
    # an API key is configured. Keys never reach the browser.
    key_attr_names = {"claude": "ANTHROPIC_API_KEY"}
    for key, cfg in CLOUD_PROVIDERS.items():
        entry = {
            "label": cfg["label"],
            "models": cfg["models"],
            "current": cfg["default_model"],
            "available": bool(cfg["api_key"]),
        }
        if not cfg["api_key"]:
            attr_name = key_attr_names.get(key, key.upper() + "_API_KEY")
            entry["error"] = f"set {attr_name} in config.py to enable {cfg['label']}"
        result[key] = entry

    return result


@app.route("/api/index_repo", methods=["POST"])
def index_repo():
    session_id = get_session_id()
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return {"error": "path is required"}, 400
    try:
        result = repo_index.start_indexing(session_id, path)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"result": result}


@app.route("/api/index_status")
def index_status():
    return repo_index.get_status(get_session_id())


# ---- Provider-specific streaming generators ------------------------------

def stream_ollama(model, messages):
    try:
        with requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={"model": model, "messages": messages, "stream": True},
            stream=True,
            timeout=300,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break
    except requests.RequestException as e:
        yield f"\n\n[error contacting Ollama at {OLLAMA_HOST}: {e}]"


def stream_openai_compatible(provider_key, cfg, model, messages):
    """Shared by Kimi and OpenAI — both speak the same Chat Completions
    SSE format ('data: {...}' lines, ending in 'data: [DONE]')."""
    if not cfg["api_key"]:
        yield f"\n\n[error: {cfg['label']} selected but its API key isn't set in config.py]"
        return

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with requests.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={"model": model, "messages": messages, "stream": True},
                stream=True,
                timeout=300,
            ) as r:
                if r.status_code != 200:
                    err_type, hint, retryable = _classify_error(r)
                    if retryable and attempt < MAX_ATTEMPTS:
                        time.sleep(2 * attempt)
                        continue
                    yield (
                        f"\n\n[{cfg['label']} API error {r.status_code}"
                        f"{f' ({err_type})' if err_type else ''}: {r.text}{hint}]"
                    )
                    return

                for line in r.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload.strip() == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    piece = delta.get("content", "")
                    if piece:
                        yield piece
                return
        except requests.RequestException as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            yield f"\n\n[error contacting {cfg['label']} at {cfg['base_url']}: {e}]"
            return


def stream_claude(cfg, model, system_prompt, chat_messages):
    """Anthropic Messages API — system prompt is a separate field (not a
    message), and streaming is SSE with typed events, not raw 'data:' deltas."""
    if not cfg["api_key"]:
        yield f"\n\n[error: Claude selected but ANTHROPIC_API_KEY isn't set in config.py]"
        return

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with requests.post(
                f"{cfg['base_url']}/messages",
                headers={
                    "x-api-key": cfg["api_key"],
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "system": system_prompt,
                    "messages": chat_messages,
                    "max_tokens": 4096,
                    "stream": True,
                },
                stream=True,
                timeout=300,
            ) as r:
                if r.status_code != 200:
                    err_type, hint, retryable = _classify_error(r)
                    if retryable and attempt < MAX_ATTEMPTS:
                        time.sleep(2 * attempt)
                        continue
                    yield (
                        f"\n\n[Claude API error {r.status_code}"
                        f"{f' ({err_type})' if err_type else ''}: {r.text}{hint}]"
                    )
                    return

                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("type") == "content_block_delta":
                        piece = chunk.get("delta", {}).get("text", "")
                        if piece:
                            yield piece
                    elif chunk.get("type") == "message_stop":
                        break
                return
        except requests.RequestException as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            yield f"\n\n[error contacting Claude API at {cfg['base_url']}: {e}]"
            return


def stream_gemini(cfg, model, system_prompt, chat_messages):
    """Gemini API — uses 'contents' with role user/model (not assistant),
    a separate systemInstruction field, and SSE via alt=sse."""
    if not cfg["api_key"]:
        yield "\n\n[error: Gemini selected but GEMINI_API_KEY isn't set in config.py]"
        return

    contents = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in chat_messages
    ]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with requests.post(
                f"{cfg['base_url']}/models/{model}:streamGenerateContent",
                params={"alt": "sse"},
                headers={"x-goog-api-key": cfg["api_key"], "Content-Type": "application/json"},
                json={
                    "contents": contents,
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                },
                stream=True,
                timeout=300,
            ) as r:
                if r.status_code != 200:
                    err_type, hint, retryable = _classify_error(r)
                    if retryable and attempt < MAX_ATTEMPTS:
                        time.sleep(2 * attempt)
                        continue
                    yield (
                        f"\n\n[Gemini API error {r.status_code}"
                        f"{f' ({err_type})' if err_type else ''}: {r.text}{hint}]"
                    )
                    return

                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    candidates = chunk.get("candidates", [])
                    if not candidates:
                        continue
                    for part in candidates[0].get("content", {}).get("parts", []):
                        piece = part.get("text", "")
                        if piece:
                            yield piece
                return
        except requests.RequestException as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            yield f"\n\n[error contacting Gemini API at {cfg['base_url']}: {e}]"
            return


def _classify_error(r):
    """Given a non-200 requests.Response, return (error_type, hint, retryable)."""
    err_type = None
    try:
        err_type = r.json().get("error", {}).get("type")
    except (ValueError, AttributeError):
        pass

    retryable = r.status_code in RETRYABLE_STATUSES and err_type in (
        None, "rate_limit_reached_error", "engine_overloaded_error", "rate_limit_error", "overloaded_error",
    )

    hint = ""
    if r.status_code == 429:
        hint = (
            " (this can mean an actual rate limit, an overloaded server, OR "
            "— very common on a fresh key — insufficient account balance; "
            "check your billing/top-up on that provider's dashboard)"
        )
    elif r.status_code in (401, 403):
        hint = " (check that the API key is correct and active)"

    return err_type, hint, retryable


def get_generator(provider, model, system_prompt, chat_messages):
    """Return a zero-arg generator function for the given provider that
    yields text chunks. Shared by /api/chat (streamed to the client) and
    /api/propose_edit (collected fully server-side)."""
    messages_with_system = [{"role": "system", "content": system_prompt}] + chat_messages

    if provider == "ollama":
        return lambda: stream_ollama(model, messages_with_system)
    elif provider in ("kimi", "openai"):
        cfg = CLOUD_PROVIDERS[provider]
        return lambda: stream_openai_compatible(provider, cfg, model, messages_with_system)
    elif provider == "claude":
        cfg = CLOUD_PROVIDERS["claude"]
        return lambda: stream_claude(cfg, model, system_prompt, chat_messages)
    elif provider == "gemini":
        cfg = CLOUD_PROVIDERS["gemini"]
        return lambda: stream_gemini(cfg, model, system_prompt, chat_messages)
    else:
        return None


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Expects JSON: {
      "message": str,
      "history": [{"role": "user"|"assistant", "content": str}, ...],
      "provider": "ollama" | "kimi" | "openai" | "claude" | "gemini"
      "model": str  (optional; defaults to that provider's default model)
      "use_repo": bool  (optional; retrieve+attach repo context if True)
    }
    Streams back plain text chunks as the model generates them.
    """
    body = request.get_json(force=True, silent=True) or {}
    user_message = body.get("message", "").strip()
    history = body.get("history", [])
    provider = (body.get("provider") or CHAT_PROVIDER).lower()
    session_id = get_session_id()

    if provider in CLOUD_PROVIDERS:
        default_model = CLOUD_PROVIDERS[provider]["default_model"]
    else:
        default_model = OLLAMA_MODEL
    model = body.get("model") or default_model

    use_repo = bool(body.get("use_repo")) and repo_index.has_index(session_id)

    if not user_message:
        return {"error": "message is required"}, 400

    system_prompt = SYSTEM_PROMPT
    if use_repo:
        try:
            matches = repo_index.search(session_id, user_message, top_k=6)
        except requests.RequestException as e:
            matches = []
            system_prompt += f"\n\n[repo search failed: {e}]"
        if matches:
            context_blocks = [
                f"### {m['file']} (lines {m['start_line']}-{m['end_line']}, "
                f"relevance {m['score']:.2f})\n```\n{m['text']}\n```"
                for m in matches
            ]
            system_prompt += REPO_SYSTEM_SUFFIX + "\n\n" + "\n\n".join(context_blocks)

    # chat_messages excludes the system prompt — Claude/Gemini take it
    # separately; Ollama/OpenAI/Kimi get it prepended as a system message.
    chat_messages = []
    for turn in history:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            chat_messages.append({"role": role, "content": content})
    chat_messages.append({"role": "user", "content": user_message})

    generate = get_generator(provider, model, system_prompt, chat_messages)
    if generate is None:
        return {"error": f"unknown provider: {provider}"}, 400

    return Response(stream_with_context(generate()), mimetype="text/plain")


def _parse_marked_response(text):
    """Extract (explanation, content) from a model response. Explanation
    is any text before the <<<FILE>>> marker (empty string if none).
    Content is None if nothing usable was found at all."""
    start_marker = "<<<FILE>>>"
    end_marker = "<<<END>>>"
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start != -1 and end != -1 and end > start:
        explanation = text[:start].strip()
        content = text[start + len(start_marker):end]
        return explanation, content.lstrip("\n").rstrip("\n") + "\n"

    # Fallback: a single ``` fenced block, no explanation extraction here
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            block = parts[1]
            # strip a leading language tag line, e.g. "python\n..."
            lines = block.split("\n", 1)
            if len(lines) == 2 and len(lines[0].split()) <= 1:
                block = lines[1]
            return "", block.rstrip("\n") + "\n"

    return "", None


def _parse_edit_response(text):
    """Back-compat wrapper for callers that only need the content."""
    _, content = _parse_marked_response(text)
    return content


def _compute_diff(original, proposed, path):
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
    )
    result = []
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            kind = "header"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "remove"
        else:
            kind = "context"
        result.append({"type": kind, "text": line.rstrip("\n")})
    return result


@app.route("/api/propose_edit", methods=["POST"])
def propose_edit():
    """
    Expects JSON: {
      "path": str (relative to the indexed repo, or absolute — must be
               inside the currently indexed repo for this session),
      "instruction": str,
      "provider": str, "model": str  (same as /api/chat)
    }
    Reads the file, asks the model for a full rewritten version, computes
    a diff against the current content, and returns both — nothing is
    written to disk here.
    """
    session_id = get_session_id()
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "").strip()
    instruction = body.get("instruction", "").strip()
    provider = (body.get("provider") or CHAT_PROVIDER).lower()

    if not path or not instruction:
        return {"error": "path and instruction are required"}, 400

    abs_path, repo_root, err = resolve_in_repo(session_id, path)
    if err:
        return err, 400
    if not os.path.isfile(abs_path):
        return {"error": f"not a file: {path}"}, 400

    try:
        size = os.path.getsize(abs_path)
        if size > MAX_EDIT_FILE_BYTES:
            return {"error": f"file too large ({size} bytes, limit {MAX_EDIT_FILE_BYTES})"}, 400
        with open(abs_path, "r", encoding="utf-8") as f:
            original_content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"could not read file: {e}"}, 400

    rel_path = os.path.relpath(abs_path, repo_root)

    if provider in CLOUD_PROVIDERS:
        default_model = CLOUD_PROVIDERS[provider]["default_model"]
    else:
        default_model = OLLAMA_MODEL
    model = body.get("model") or default_model

    user_content = (
        f"File path: {rel_path}\n\n"
        f"Current file content:\n{original_content}\n\n"
        f"Instruction: {instruction}"
    )
    chat_messages = [{"role": "user", "content": user_content}]

    generate = get_generator(provider, model, EDIT_SYSTEM_PROMPT, chat_messages)
    if generate is None:
        return {"error": f"unknown provider: {provider}"}, 400

    raw_response = "".join(generate())
    proposed_content = _parse_edit_response(raw_response)

    if proposed_content is None:
        return {
            "error": "could not parse a file rewrite from the model's response",
            "raw_response": raw_response,
        }, 502

    # Avoid a spurious "identical line" diff hunk caused only by whether
    # the file ends with a trailing newline — match the original's convention.
    if original_content.endswith("\n") and not proposed_content.endswith("\n"):
        proposed_content += "\n"
    elif not original_content.endswith("\n") and proposed_content.endswith("\n"):
        proposed_content = proposed_content.rstrip("\n")

    diff = _compute_diff(original_content, proposed_content, rel_path)
    return {
        "path": rel_path,
        "original_content": original_content,
        "proposed_content": proposed_content,
        "diff": diff,
        "unchanged": proposed_content == original_content,
    }


@app.route("/api/apply_edit", methods=["POST"])
def apply_edit():
    """
    Expects JSON: { "path": str, "content": str }
    Writes content to path (which must be inside the session's indexed
    repo), after backing up the current file to "<path>.bak". Only
    called after the user approves a proposed diff.
    """
    session_id = get_session_id()
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "").strip()
    content = body.get("content", None)

    if not path or content is None:
        return {"error": "path and content are required"}, 400

    abs_path, repo_root, err = resolve_in_repo(session_id, path)
    if err:
        return err, 400
    if not os.path.isfile(abs_path):
        return {"error": f"not a file: {path}"}, 400

    try:
        backup_path = abs_path + ".bak"
        with open(abs_path, "r", encoding="utf-8") as f:
            current = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(current)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"error": f"could not write file: {e}"}, 500

    return {
        "ok": True,
        "path": os.path.relpath(abs_path, repo_root),
        "backup": os.path.relpath(backup_path, repo_root),
    }


DIAGNOSE_SYSTEM_PROMPT = (
    "You are an expert software engineer diagnosing and fixing a bug. "
    "You will be given a description of the problem (which may include an "
    "error message or traceback), the full current contents of the file "
    "most likely responsible, and possibly a few related snippets from "
    "elsewhere in the repository for context.\n\n"
    "First, in 2-4 sentences, explain what's actually wrong and why.\n"
    "Then, on a new line, output the complete corrected contents of that "
    "file, wrapped EXACTLY like this, with nothing after it:\n"
    "<<<FILE>>>\n"
    "...entire corrected file content here...\n"
    "<<<END>>>\n\n"
    "Preserve everything in the file that isn't related to the bug — do "
    "not reformat, reorder, or rewrite unrelated code. If the provided "
    "file doesn't actually seem to contain the bug, say so plainly in "
    "your explanation and still return that file's content unchanged "
    "between the markers."
)


@app.route("/api/diagnose", methods=["POST"])
def diagnose():
    """
    Expects JSON: { "problem": str, "provider": str, "model": str }
    Searches the session's indexed repo for the file(s) most relevant to
    the described problem, asks the model to diagnose and fix the most
    likely one, and returns an explanation + diff — same review-before-
    apply flow as /api/propose_edit (use /api/apply_edit to write it).
    """
    session_id = get_session_id()
    body = request.get_json(force=True, silent=True) or {}
    problem = body.get("problem", "").strip()
    provider = (body.get("provider") or CHAT_PROVIDER).lower()

    if not problem:
        return {"error": "problem description is required"}, 400
    if not repo_index.has_index(session_id):
        return {"error": "no repo indexed for this session — index a repo first"}, 400

    repo_root = repo_index.get_repo_root(session_id)

    try:
        matches = repo_index.search(session_id, problem, top_k=8)
    except requests.RequestException as e:
        return {"error": f"repo search failed: {e}"}, 502

    if not matches:
        return {"error": "no relevant code found in the indexed repo for this problem"}, 404

    # Primary suspect = file containing the single highest-scoring chunk.
    primary_file = matches[0]["file"]
    abs_path, _, err = resolve_in_repo(session_id, primary_file)
    if err or not os.path.isfile(abs_path):
        return {"error": f"resolved suspect file is invalid: {primary_file}"}, 500

    try:
        size = os.path.getsize(abs_path)
        if size > MAX_EDIT_FILE_BYTES:
            return {"error": f"suspect file too large ({size} bytes, limit {MAX_EDIT_FILE_BYTES})"}, 400
        with open(abs_path, "r", encoding="utf-8") as f:
            original_content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"could not read suspect file: {e}"}, 400

    # A little extra context from other relevant files, for the model —
    # doesn't mean those files can be edited, just informs the fix.
    context_blocks = []
    seen_files = set()
    for m in matches:
        if m["file"] == primary_file or m["file"] in seen_files:
            continue
        seen_files.add(m["file"])
        context_blocks.append(
            f"### {m['file']} (lines {m['start_line']}-{m['end_line']})\n```\n{m['text']}\n```"
        )
        if len(seen_files) >= 3:
            break

    user_content = (
        f"Problem description:\n{problem}\n\n"
        f"Most likely responsible file: {primary_file}\n\n"
        f"Full current content of {primary_file}:\n{original_content}\n"
    )
    if context_blocks:
        user_content += "\n\nOther potentially related code from the repo:\n\n" + "\n\n".join(context_blocks)

    if provider in CLOUD_PROVIDERS:
        default_model = CLOUD_PROVIDERS[provider]["default_model"]
    else:
        default_model = OLLAMA_MODEL
    model = body.get("model") or default_model

    chat_messages = [{"role": "user", "content": user_content}]
    generate = get_generator(provider, model, DIAGNOSE_SYSTEM_PROMPT, chat_messages)
    if generate is None:
        return {"error": f"unknown provider: {provider}"}, 400

    raw_response = "".join(generate())
    explanation, proposed_content = _parse_marked_response(raw_response)

    if proposed_content is None:
        return {
            "error": "could not parse a fix from the model's response",
            "raw_response": raw_response,
        }, 502

    if original_content.endswith("\n") and not proposed_content.endswith("\n"):
        proposed_content += "\n"
    elif not original_content.endswith("\n") and proposed_content.endswith("\n"):
        proposed_content = proposed_content.rstrip("\n")

    diff = _compute_diff(original_content, proposed_content, primary_file)
    return {
        "path": primary_file,
        "explanation": explanation,
        "original_content": original_content,
        "proposed_content": proposed_content,
        "diff": diff,
        "unchanged": proposed_content == original_content,
        "other_candidates": [m["file"] for m in matches if m["file"] != primary_file][:3],
    }


@app.route("/api/run_tests", methods=["POST"])
def run_tests():
    """
    Runs config.TEST_COMMAND in the session's indexed repo root and
    returns pass/fail + captured output. Command comes only from
    config.py (never from the request body) so the browser can't inject
    an arbitrary command to run on the server.
    """
    session_id = get_session_id()
    repo_root = repo_index.get_repo_root(session_id)
    if not repo_root:
        return {"error": "no repo indexed for this session — index a repo first"}, 400

    command = (getattr(config, "TEST_COMMAND", "") or "").strip()
    if not command:
        return {"error": "no TEST_COMMAND set in config.py — add one to enable this"}, 400

    timeout = getattr(config, "TEST_TIMEOUT_SECONDS", 120)

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"test command timed out after {timeout}s", "command": command}, 504
    except OSError as e:
        return {"error": f"could not run test command: {e}", "command": command}, 500

    return {
        "ok": True,
        "command": command,
        "exit_code": proc.returncode,
        "passed": proc.returncode == 0,
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-20000:],
    }


if __name__ == "__main__":
    app.run(debug=True, port=5000)