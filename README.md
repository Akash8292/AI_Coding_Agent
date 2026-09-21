# ⚡ CodeSage — Production-Quality AI Coding Agent Platform

CodeSage is a modern, full-stack AI engineering assistant and developer workspace inspired by Cursor and Google Antigravity. It empowers developers to understand large repositories, investigate complex bugs, review diffs line-by-line, and safely propose and apply code changes with granular permission controls.

---

## 🌟 Key Features

* **Multi-Provider LLM Abstraction**:
  * Cloud: **OpenAI** (`gpt-4o`, `gpt-4o-mini`), **Anthropic** (`claude-3-5-sonnet`), **Google Gemini** (`gemini-1.5-pro`, `gemini-1.5-flash`), **OpenRouter** (200+ models), **Kimi / Moonshot**.
  * Local: **Ollama** (`codellama`, `deepseek-coder`, `llama3`).
  * Cloud fallback without requiring local Ollama installed or running.
* **Repository Intelligence**:
  * Fast token-aware indexing with intelligent ignore filters (`.gitignore`, binaries, virtual environments).
  * Fast keyword & BM25 retrieval + optional vector embeddings.
  * Real-time file tree browsing and code navigation.
* **Agent & Tool System**:
  * **Intent Detection**: Automatically distinguishes general programming questions from repository investigation and code modification requests.
  * **Unified Diff Viewer**: Visual side-by-side / unified diff representation with line-by-line additions, deletions, hunk headers, and single-click Accept / Reject.
  * **Git Safety & Checkpoints**: Automatic or manual git checkpoints (stashes) prior to applying edits, with instant rollbacks.
  * **Permission System**: Explicit user confirmation before local command execution, with single-run and session-wide approvals.
* **Modern Developer SPA**:
  * Sleek dark mode design system built with custom CSS tokens.
  * Streaming responses via Server-Sent Events (SSE).
  * Quick Mode & Deep Investigation Mode.
  * Command Palette (`Ctrl+K` / `Cmd+K`) for rapid workspace and chat navigation.
  * Real-time Activity Log displaying inspection steps.
  * Token & Cost Usage Tracker.
  * Multi-user JWT authentication.

---

## 🏗️ Architecture

```
AI_Coding_Agent/
├── backend/
│   ├── app/
│   │   ├── agent/            # Intent detection, context builder, planner, tools, executor
│   │   ├── api/              # Chat SSE, repositories, conversations, usage, health
│   │   ├── auth/             # JWT authentication routes & utilities
│   │   ├── llm/              # Unified provider interfaces (OpenAI, Claude, Gemini, etc.)
│   │   ├── models/           # SQLAlchemy models (User, Workspace, Conversation, Usage)
│   │   ├── repository/       # Indexer, searcher, git operations
│   │   ├── config.py         # 12-factor environment configuration
│   │   └── database.py       # Database connection & session management
│   ├── requirements.txt      # Lightweight backend dependencies
│   └── wsgi.py               # WSGI entry point
├── frontend/
│   ├── index.html            # Single-page application HTML
│   ├── style.css             # Developer-grade dark UI styling system
│   └── src/
│       ├── api.js            # REST & SSE chat streaming client
│       ├── store.js          # Centralized reactive state store
│       ├── main.js           # UI coordinator & event listeners
│       └── components/       # DiffViewer, ModelSelector, ActivityLog, CommandPalette
├── Dockerfile                # Production container spec
├── docker-compose.yml        # Docker compose specification
├── render.yaml               # 1-click Render blueprint
└── .env.example              # Configuration template
```

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+
- Git installed on your system

### 2. Setup Environment
```bash
# Clone or navigate to the repository
cd AI_Coding_Agent

# Create and activate virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r backend/requirements.txt
```

### 3. Configure API Keys
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Set at least one cloud provider API key (e.g., `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or `KIMI_API_KEY`).

### 4. Run Locally
```bash
python backend/wsgi.py
```
Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## 🐳 Docker Deployment

To launch CodeSage via Docker:
```bash
docker compose up --build
```
Access the application at `http://localhost:5000`.

---

## 🧪 Running Tests

```bash
pytest backend/tests -v
```

---

## 🔒 Security & Safety Controls

1. **Sandboxed Command Execution**: Local system commands are blocked until you explicitly click **Allow Once** or **Allow for Session**.
2. **Atomic Git Checkpoints**: Any automated file modification automatically backs up uncommitted states to a safe Git stash checkpoint.
3. **Diff Verification**: All AI-proposed edits are computed and displayed as diffs before touching disk.
4. **JWT Authentication**: User passwords are encrypted with bcrypt and all API requests require bearer tokens.
