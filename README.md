# CodeSage

CodeSage is a self-hosted AI coding agent for real repositories. Ask questions about a codebase and get answers grounded in the actual files, or ask for a change and get a **validated diff that nothing touches your files until you accept it**.

It is fast for simple requests (naming a file skips repository search entirely), searches the repository when a question needs it, and is conservative with your code: every edit is reviewed, stale-checked against the file on disk, applied atomically, verified, and revertible.

---

## Features

- **Scope-aware requests.** Each request is classified into a *mode* (answer / edit / command) and the *minimum* context it needs:
  - `explicit_file(s)` – the message names files → only those files are read
  - `repository` – search the index and read the relevant sections (named files stay pinned)
  - `general` – no repository context
  - `command` – propose a shell command that needs your permission
- **Structured edits, not text scraping.** The model returns a JSON edit plan (`insert` / `replace` / `replace_lines` / `delete` / `create` …). CodeSage applies it to the original file deterministically, validates it (unique anchors, no overlaps, syntax check), and computes the diff itself. Invalid plans get one automatic repair round-trip with the exact error.
- **Review before write.** Accept / Reject / Revert per proposal. Accept re-checks each file's hash, so edits you made after the proposal are never overwritten; writes are atomic and all-or-nothing, followed by verification.
- **Real streaming activity.** The timeline only shows operations that actually happened, with measured durations (`Read demo.py (24 lines) · 1 ms`).
- **Stop that actually stops.** Stop cancels the server run and aborts the provider HTTP request (≈0.1 s in testing); the conversation stays usable.
- **Timeouts everywhere.** Connect, first-token, idle and total deadlines per LLM call; timeouts on search, git, commands and clones.
- **Multiple providers:** Gemini, OpenAI, Anthropic, OpenRouter, Kimi, and optional Ollama, all behind one interface with typed errors, real token usage and cost estimates.
- **Git-aware.** Branch/status/log panel, warnings when a target file has uncommitted changes, and non-destructive checkpoints (`git stash create` + `store`; your working tree is never stashed away).
- **Multi-user.** JWT auth; conversations, workspaces, proposals, commands and usage are scoped per user; per-user rate, token and concurrency limits.

## Architecture

```
┌────────────── Browser (frontend/, vanilla JS) ──────────────┐
│ chat · activity timeline · diff review cards · git/files    │
└───────────────┬───────────────────────────────▲─────────────┘
    POST /api/chat (SSE)  /api/changes/*  /api/commands/*
┌───────────────▼───────────────────────────────┴─────────────┐
│ app/api/chat.py   auth · limits · run registry · persistence│
│        │                                                    │
│ app/agent/runner.py  ── AgentRun ──────────────────────────┐│
│   classify ─▶ gather context ─▶ LLM ─▶ answer│edit│command ││
│   (context.py)  (workspace_fs,    (worker thread +         ││
│                  searcher, git)    heartbeats + cancel)     ││
│                                      │edit                 ││
│                         editor.py: parse → apply → validate ││
│                                     → diff (never the LLM)  ││
│ app/agent/executor.py  apply/revert: hash check → checkpoint││
│                        → atomic write → verify             ││
│ app/llm/  LLMProvider ◀─ Gemini · OpenAI · Anthropic ·      ││
│           OpenRouter · Kimi · Ollama (optional)             ││
└──────────────────────────────────────────────────────────────┘
          SQLite / PostgreSQL          user repositories (per-user folders)
```

### Request flow

```
USER ─▶ classify (mode + scope) ─▶ minimal context ─▶ LLM
                                                     │
          answer ◀───────────────────────────────────┤ stream text
          edit   ◀─ JSON plan ─▶ validate/apply in memory ─▶ diff ─▶ REVIEW
                                                                 ├─ Accept ─▶ hash check ─▶ write ─▶ verify
                                                                 └─ Reject ─▶ nothing written
          command ◀─ JSON {command} ─▶ safety policy ─▶ approval ─▶ sandboxed run
```

### Key design decisions

| Concern | Decision |
|---|---|
| Edits | The LLM describes operations; CodeSage computes the file and the diff, so the diff is exactly what gets written. |
| Stale files | Proposals store the sha256 of each original; Accept refuses if the file changed since (status `stale`, nothing written). |
| Cancellation | Provider HTTP runs on a reader thread consumed via a queue polled every 100 ms, so Stop and deadlines work even while a socket read is blocked (also on Windows). |
| Errors | Providers raise typed `ProviderError`s (`auth`, `quota`, `rate_limit`, `timeout`, …); they are never streamed as answer text. Retries happen only before output and only when they can help. |
| Process model | One gunicorn process with many threads (`gthread`): the in-memory run registry must see the cancel request. |

## Supported LLM providers

| Provider | Setting | Notes |
|---|---|---|
| Google Gemini | `GEMINI_API_KEY` | Model list fetched live. Simple single-file edits use low thinking automatically (measured: ~3 s vs 5–84 s). |
| OpenAI | `OPENAI_API_KEY` | JSON mode for edits, streamed usage. |
| Anthropic | `ANTHROPIC_API_KEY` | Messages API streaming with usage. |
| OpenRouter | `OPENROUTER_API_KEY` | OpenAI-compatible. |
| Kimi (Moonshot) | `KIMI_API_KEY` | OpenAI-compatible. |
| Ollama | `OLLAMA_ENABLED=true` | **Optional** local provider; never required, probed with short timeouts. |

Placeholder keys like `sk-...` are treated as "not configured". A provider whose key is rejected, or a model whose quota is exhausted, is flagged in the model selector.

## Local development

Requirements: Python 3.10+ and git.

```bash
python -m venv venv
source venv/bin/activate            # Windows: .\venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example .env                # add at least one provider key
```

### Running the backend (serves the frontend too)

```bash
python backend/wsgi.py              # http://127.0.0.1:5000
DEBUG=true python backend/wsgi.py   # auto-reload + debugger (localhost only)
```

### Running the frontend

The frontend is static HTML/JS in `frontend/` and is served by the backend; there is no build step. To serve it from a different origin, set `window.CODESAGE_API_URL` before `src/api.js` loads and add that origin to `CORS_ORIGINS`.

Open the app, create an account, add a workspace (a local directory path, or a git URL to clone), and start asking.

## Environment variables

See [`.env.example`](.env.example) for the full, commented list. The essentials:

| Variable | Purpose |
|---|---|
| `FLASK_ENV` | `production` enables strict defaults (required `JWT_SECRET`, workspace isolation, same-origin CORS) |
| `JWT_SECRET` | Token signing key — **required in production** |
| `DATABASE_URL` | SQLite (default) or `postgresql://…` |
| `*_API_KEY` | Provider keys (at least one) |
| `LLM_*_TIMEOUT` | Connect / first-token / idle / total deadlines |
| `WORKSPACE_ROOT`, `ALLOW_ANY_WORKSPACE_PATH`, `ALLOW_GIT_CLONE` | Where user repositories may live |
| `MAX_REQUESTS_PER_MINUTE`, `MAX_TOKENS_PER_DAY`, `MAX_CONCURRENT_REQUESTS_PER_USER` | Per-user limits |

## Running tests

```bash
pip install -r backend/requirements-dev.txt
cd backend && python -m pytest -q
```

The suite (123 tests) covers:
- **Providers over real sockets** (`test_providers.py`): a local fake LLM server exercises streaming, usage, error classification, retry policy, first-token/idle timeouts, and cancelling a blocked read.
- **Edit engine** (`test_editor.py`): operation semantics, ambiguity/overlap rejection, syntax validation, diffs.
- **Scope detection** (`test_scope.py`).
- **End-to-end workflows** (`test_workflows.py`): HTTP → SSE → runner → provider → persistence with a scripted LLM — all mandatory scenarios (explain project/file, edit→accept, reject, stale protection, revert, repair loop, stop + continue, repository search, multi-file refactor, provider errors, commands, multi-user isolation, path policy).
- **Database migration** of pre-existing schemas (`test_migration.py`).

## Docker

```bash
cp .env.example .env     # set JWT_SECRET and a provider key
docker compose up --build
# optional local models:  docker compose --profile ollama up --build   (and OLLAMA_ENABLED=true)
```

All state (database, indexes, cloned workspaces) lives in the `codesage_data` volume at `/app/data`. The image runs as a non-root user and never includes `.env`, local databases or caches (see `.dockerignore`).

## Production deployment

- Run with gunicorn using `backend/gunicorn.conf.py`: **one process, many threads** (`gthread`). Stop/cancel and SSE rely on the run living in the process that receives the cancel request. Scale by adding threads, or add instances behind a load balancer with sticky sessions.
- Set `FLASK_ENV=production` and `JWT_SECRET`. Startup fails without the secret.
- Use PostgreSQL (`DATABASE_URL`) for anything beyond a single small instance.
- Put the service behind HTTPS. Disable proxy buffering for `/api/chat` (the app sends `X-Accel-Buffering: no` and heartbeats every second).
- `GET /health` reports database and provider status (no outbound network calls).

## Render deployment

`render.yaml` is a Blueprint: **New → Blueprint →** select the repo. It builds the Dockerfile, mounts a persistent disk at `/app/data`, generates `JWT_SECRET`, and asks for provider keys. Persistent disks need a paid instance type; alternatively attach Render PostgreSQL and set `DATABASE_URL` (cloned workspaces still need the disk). On Render, users add repositories by **git URL**; arbitrary server paths are disabled.

## Ollama (optional)

```bash
ollama serve && ollama pull codellama
# .env
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=codellama
```

If Ollama is disabled or not running, it shows as unavailable in the model selector and requests to it fail in about a second with a clear message. Cloud providers are unaffected.

## Security considerations

- **Nothing is written without approval**; approval re-validates paths and file hashes server-side.
- **Path policy:** all file access goes through `app/repository/workspace_fs.py`. It rejects absolute paths, `..` and symlink escapes, `.git` internals, secret files (`.env`, keys, credentials) and binaries. Secret files are also never sent to an LLM.
- **Workspace isolation:** in production, users can only use folders under `WORKSPACE_ROOT/<their id>/`; every API query is scoped to the authenticated user, including cancel.
- **Commands:** never run without explicit approval. Destructive patterns (`rm -rf`, `git reset --hard`, `curl | sh`, privilege escalation, …) are blocked outright. Commands run without a shell, in the workspace, with API keys and secrets stripped from the environment, a hard timeout that kills the process tree, and capped output. On a shared server, command execution still runs as the server's OS user, so use container isolation per deployment.
- **LLM output is untrusted:** markdown is sanitized with DOMPurify, and a CSP restricts scripts to the app and cdnjs.
- **Git checkpoints** never modify your working tree; restoring one snapshots the current state first.
- API keys never reach the browser and are never logged.

## Project structure

```
backend/
  app/
    agent/
      context.py      request classification (mode/scope), history budgeting, overview
      runner.py       AgentRun pipeline: context → LLM → answer | edit | command
      editor.py       edit-plan schema, deterministic application, validation, diffs
      executor.py     apply / revert reviewed changes (hash check, atomic, verify)
      permissions.py  command risk policy and sandboxed execution
      planner.py      prompts for each mode
    api/              chat (SSE), changes & commands, workspaces, conversations, usage, health
    llm/              LLMProvider base (streaming core, errors, deadlines) + providers + factory
    repository/       workspace_fs (safe file access), indexer/searcher (BM25), git_tools
    models/           User, Workspace, Conversation, Message, ProposedChange, PendingApproval, UsageRecord
  gunicorn.conf.py
  tests/
frontend/
  index.html  style.css
  src/  api.js  store.js  main.js
        components/  Proposals.js  DiffViewer.js  ActivityLog.js  ModelSelector.js  CommandPalette.js
Dockerfile  docker-compose.yml  render.yaml  .env.example
```

`app.py`, `repo_index.py`, `static/` and `templates/` at the repository root are the earlier single-file prototype. They are not used by the current application or the Docker image.

## Future improvements

- Agentic multi-step tool loops (read → search → read more) for very large refactors, instead of one context-gathering pass.
- Hunk-level accept/reject within a proposal.
- Running targeted tests automatically after an edit (currently syntax verification plus optional user-approved commands).
- Shared run registry (e.g. Redis) so cancellation works across multiple processes/instances.
- Per-user sandboxed command execution (container per workspace).
- Exact token counting with provider tokenizers when a provider doesn't report usage.
