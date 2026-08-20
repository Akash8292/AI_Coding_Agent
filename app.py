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

import json
import os
import time

import requests
from flask import Flask, Response, render_template, request, stream_with_context

import repo_index

app = Flask(__name__)

# ---- Config -----------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "codellama")

CHAT_PROVIDER = os.environ.get("CHAT_PROVIDER", "ollama").lower()

# Cloud providers. Model lists are curated defaults (checked Aug 2026) —
# lineups move fast, so override via the *_MODEL env var if a name has
# changed, or just type a different model string in the API request.
CLOUD_PROVIDERS = {
    "kimi": {
        "label": "Kimi (Moonshot)",
        "api_key": os.environ.get("KIMI_API_KEY", ""),
        "base_url": os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
        "default_model": os.environ.get("KIMI_MODEL", "kimi-k2.7-code"),
        "models": ["kimi-k2.7-code", "kimi-k2.6", "kimi-k3"],
    },
    "openai": {
        "label": "OpenAI",
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "default_model": os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        "default_model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
        "models": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
    },
    "gemini": {
        "label": "Gemini (Google)",
        "api_key": os.environ.get("GEMINI_API_KEY", ""),
        "base_url": os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        "default_model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
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
    # an API key is configured on the server. Keys never reach the browser.
    env_var_names = {"claude": "ANTHROPIC_API_KEY"}
    for key, cfg in CLOUD_PROVIDERS.items():
        entry = {
            "label": cfg["label"],
            "models": cfg["models"],
            "current": cfg["default_model"],
            "available": bool(cfg["api_key"]),
        }
        if not cfg["api_key"]:
            entry["error"] = f"{env_var_names.get(key, key.upper() + '_API_KEY')} not set on the server"
        result[key] = entry

    return result


@app.route("/api/index_repo", methods=["POST"])
def index_repo():
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return {"error": "path is required"}, 400
    try:
        result = repo_index.start_indexing(path)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"result": result}


@app.route("/api/index_status")
def index_status():
    return repo_index.get_status()


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
        yield f"\n\n[error: {cfg['label']} selected but {provider_key.upper()}_API_KEY is not set on the server]"
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
        yield f"\n\n[error: Claude selected but ANTHROPIC_API_KEY is not set on the server]"
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
        yield "\n\n[error: Gemini selected but GEMINI_API_KEY is not set on the server]"
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

    if provider in CLOUD_PROVIDERS:
        default_model = CLOUD_PROVIDERS[provider]["default_model"]
    else:
        default_model = OLLAMA_MODEL
    model = body.get("model") or default_model

    use_repo = bool(body.get("use_repo")) and repo_index.has_index()

    if not user_message:
        return {"error": "message is required"}, 400

    system_prompt = SYSTEM_PROMPT
    if use_repo:
        try:
            matches = repo_index.search(user_message, top_k=6)
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

    messages_with_system = [{"role": "system", "content": system_prompt}] + chat_messages

    if provider == "ollama":
        generate = lambda: stream_ollama(model, messages_with_system)
    elif provider in ("kimi", "openai"):
        cfg = CLOUD_PROVIDERS[provider]
        generate = lambda: stream_openai_compatible(provider, cfg, model, messages_with_system)
    elif provider == "claude":
        cfg = CLOUD_PROVIDERS["claude"]
        generate = lambda: stream_claude(cfg, model, system_prompt, chat_messages)
    elif provider == "gemini":
        cfg = CLOUD_PROVIDERS["gemini"]
        generate = lambda: stream_gemini(cfg, model, system_prompt, chat_messages)
    else:
        return {"error": f"unknown provider: {provider}"}, 400

    return Response(stream_with_context(generate()), mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True, port=5000)