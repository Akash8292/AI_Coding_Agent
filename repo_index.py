"""
Repo indexing for Phase 2.

Walks a local repo, chunks source files by lines, embeds each chunk with
an Ollama embedding model, and stores the vectors in memory (+ on disk
cache) so we can do similarity search against a user's question.
"""

import hashlib
import json
import os
import pickle
import threading
import time

import numpy as np
import requests

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", ".next", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", "index_cache",
}
ALLOWED_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".sql", ".html", ".css", ".scss", ".md", ".json",
    ".yaml", ".yml",
}
MAX_FILE_BYTES = 500_000
CHUNK_LINES = 60
CHUNK_OVERLAP = 10

CACHE_DIR = os.path.join(os.path.dirname(__file__), "index_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ---- Global state (single-user local tool, so this is fine) -----------
_lock = threading.Lock()
STATE = {
    "status": "idle",       # idle | indexing | done | error
    "path": None,
    "total_files": 0,
    "indexed_files": 0,
    "total_chunks": 0,
    "error": None,
}
DATA = {"chunks": [], "embeddings": None}  # embeddings: np.ndarray (n, d)


def _cache_path(repo_path):
    h = hashlib.sha256(os.path.abspath(repo_path).encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.pkl")


def _iter_files(repo_path):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_EXT:
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield fpath


def _chunk_file(fpath, repo_path):
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (UnicodeDecodeError, OSError):
        return []

    if not lines:
        return []

    rel = os.path.relpath(fpath, repo_path)
    chunks = []
    step = max(CHUNK_LINES - CHUNK_OVERLAP, 1)
    for start in range(0, len(lines), step):
        end = min(start + CHUNK_LINES, len(lines))
        text = "".join(lines[start:end]).strip()
        if text:
            chunks.append({
                "file": rel,
                "start_line": start + 1,
                "end_line": end,
                "text": text,
            })
        if end >= len(lines):
            break
    return chunks


def _embed(text):
    resp = requests.post(
        f"{OLLAMA_HOST}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _run_index(repo_path):
    with _lock:
        STATE.update(status="indexing", path=repo_path, total_files=0,
                      indexed_files=0, total_chunks=0, error=None)

    try:
        files = list(_iter_files(repo_path))
        with _lock:
            STATE["total_files"] = len(files)

        all_chunks = []
        for fpath in files:
            all_chunks.extend(_chunk_file(fpath, repo_path))
            with _lock:
                STATE["indexed_files"] += 1

        embeddings = []
        for i, ch in enumerate(all_chunks):
            header = f"# File: {ch['file']} (lines {ch['start_line']}-{ch['end_line']})\n"
            vec = _embed(header + ch["text"])
            embeddings.append(vec)
            with _lock:
                STATE["total_chunks"] = i + 1

        emb_array = np.array(embeddings, dtype=np.float32) if embeddings else np.zeros((0, 0))

        with _lock:
            DATA["chunks"] = all_chunks
            DATA["embeddings"] = emb_array
            STATE["status"] = "done"

        with open(_cache_path(repo_path), "wb") as f:
            pickle.dump({"chunks": all_chunks, "embeddings": emb_array}, f)

    except Exception as e:  # noqa: BLE001
        with _lock:
            STATE["status"] = "error"
            STATE["error"] = str(e)


def start_indexing(repo_path):
    repo_path = os.path.abspath(repo_path)
    if not os.path.isdir(repo_path):
        raise ValueError(f"Not a directory: {repo_path}")

    cached = _cache_path(repo_path)
    if os.path.exists(cached):
        with open(cached, "rb") as f:
            saved = pickle.load(f)
        with _lock:
            DATA["chunks"] = saved["chunks"]
            DATA["embeddings"] = saved["embeddings"]
            STATE.update(status="done", path=repo_path,
                          total_files=len(saved["chunks"]),
                          indexed_files=len(saved["chunks"]),
                          total_chunks=len(saved["chunks"]), error=None)
        return "cached"

    t = threading.Thread(target=_run_index, args=(repo_path,), daemon=True)
    t.start()
    return "started"


def get_status():
    with _lock:
        return dict(STATE)


def has_index():
    with _lock:
        return DATA["embeddings"] is not None and len(DATA["chunks"]) > 0


def search(query, top_k=6):
    with _lock:
        chunks = DATA["chunks"]
        embeddings = DATA["embeddings"]

    if embeddings is None or len(chunks) == 0:
        return []

    q_vec = np.array(_embed(query), dtype=np.float32)
    q_norm = q_vec / (np.linalg.norm(q_vec) + 1e-8)
    m_norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8)
    sims = m_norm @ q_norm
    top_idx = np.argsort(-sims)[:top_k]
    return [{**chunks[i], "score": float(sims[i])} for i in top_idx]