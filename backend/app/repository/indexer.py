"""
Repository file indexer.
Walks a repo, chunks source files, and optionally generates embeddings.
Primary mode: keyword indexing (no external dependencies).
Optional: semantic embeddings via OpenAI or Ollama.

Per-workspace state is stored on disk (not in RAM), so it survives restarts.
"""
import hashlib
import json
import math
import os
import pickle
import re
import threading
from collections import defaultdict
from typing import Optional

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", ".next", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", "index_cache",
    ".tox", ".eggs", "*.egg-info",
}
ALLOWED_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".sql", ".html", ".css", ".scss", ".md", ".json",
    ".yaml", ".yml", ".toml", ".env.example",
}
MAX_FILE_BYTES = 500_000
CHUNK_LINES = 60
CHUNK_OVERLAP = 10

# Per-workspace in-progress status (not persisted)
_status_lock = threading.Lock()
_status: dict = {}  # workspace_id -> status dict


def _get_cache_dir(cfg=None) -> str:
    if cfg:
        d = cfg.get("INDEX_CACHE_DIR", "")
        if d:
            os.makedirs(d, exist_ok=True)
            return d
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "index_cache")
    os.makedirs(default, exist_ok=True)
    return default


def _cache_path(workspace_id: int, repo_path: str, cfg=None) -> str:
    h = hashlib.sha256(f"{workspace_id}:{os.path.abspath(repo_path)}".encode()).hexdigest()[:16]
    return os.path.join(_get_cache_dir(cfg), f"ws_{workspace_id}_{h}.pkl")


def _iter_files(repo_path: str):
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


def _chunk_file(fpath: str, repo_path: str) -> list[dict]:
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    if not lines:
        return []

    rel = os.path.relpath(fpath, repo_path).replace("\\", "/")
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


def _build_keyword_index(chunks: list[dict]) -> dict:
    """Build a simple inverted index for BM25-style keyword search."""
    # Tokenize
    def tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())

    # Document frequency
    df = defaultdict(int)
    doc_tokens = []
    for chunk in chunks:
        tokens = set(tokenize(chunk["text"] + " " + chunk["file"]))
        doc_tokens.append(list(tokenize(chunk["text"] + " " + chunk["file"])))
        for t in tokens:
            df[t] += 1

    N = len(chunks)
    avg_dl = sum(len(t) for t in doc_tokens) / max(N, 1)

    return {"df": dict(df), "doc_tokens": doc_tokens, "N": N, "avg_dl": avg_dl}


def bm25_score(query_tokens: list[str], doc_tokens: list[str],
               df: dict, N: int, avg_dl: float,
               k1: float = 1.5, b: float = 0.75) -> float:
    dl = len(doc_tokens)
    tf_map = defaultdict(int)
    for t in doc_tokens:
        tf_map[t] += 1

    score = 0.0
    for term in query_tokens:
        if term not in tf_map:
            continue
        tf = tf_map[term]
        idf = math.log((N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
    return score


def get_status(workspace_id: int) -> dict:
    with _status_lock:
        return dict(_status.get(workspace_id, {"status": "idle"}))


def _set_status(workspace_id: int, **kwargs):
    with _status_lock:
        if workspace_id not in _status:
            _status[workspace_id] = {"status": "idle"}
        _status[workspace_id].update(kwargs)


def start_indexing(workspace_id: int, repo_path: str, cfg=None,
                   on_complete=None) -> str:
    """
    Start background indexing for workspace_id.
    Returns 'cached' if a fresh cache exists, 'started' otherwise.
    on_complete: optional callback(workspace_id, total_files, total_chunks)
    """
    repo_path = os.path.abspath(repo_path)
    if not os.path.isdir(repo_path):
        raise ValueError(f"Not a directory: {repo_path}")

    cache_file = _cache_path(workspace_id, repo_path, cfg)
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                cached = pickle.load(f)
            _set_status(workspace_id, status="done", path=repo_path,
                        total_files=cached.get("total_files", 0),
                        total_chunks=len(cached.get("chunks", [])),
                        error=None)
            if on_complete:
                on_complete(workspace_id, cached.get("total_files", 0),
                            len(cached.get("chunks", [])))
            return "cached"
        except Exception:
            pass  # Fall through to re-index

    _set_status(workspace_id, status="indexing", path=repo_path,
                total_files=0, indexed_files=0, total_chunks=0, error=None)

    t = threading.Thread(
        target=_run_index,
        args=(workspace_id, repo_path, cache_file, cfg, on_complete),
        daemon=True,
    )
    t.start()
    return "started"


def _run_index(workspace_id: int, repo_path: str, cache_file: str,
               cfg=None, on_complete=None):
    try:
        files = list(_iter_files(repo_path))
        _set_status(workspace_id, total_files=len(files))

        all_chunks = []
        for i, fpath in enumerate(files):
            all_chunks.extend(_chunk_file(fpath, repo_path))
            _set_status(workspace_id, indexed_files=i + 1, total_chunks=len(all_chunks))

        keyword_index = _build_keyword_index(all_chunks)

        # Optional: generate embeddings
        embeddings = None
        embedding_provider = (cfg or {}).get("EMBEDDING_PROVIDER", "none") if cfg else "none"
        if embedding_provider == "openai" and cfg and cfg.get("OPENAI_API_KEY"):
            embeddings = _embed_openai(all_chunks, cfg)
        elif embedding_provider == "ollama":
            embeddings = _embed_ollama(all_chunks, cfg)

        data = {
            "chunks": all_chunks,
            "keyword_index": keyword_index,
            "embeddings": embeddings,
            "total_files": len(files),
            "repo_path": repo_path,
        }

        with open(cache_file, "wb") as f:
            pickle.dump(data, f)

        _set_status(workspace_id, status="done", total_chunks=len(all_chunks))
        if on_complete:
            on_complete(workspace_id, len(files), len(all_chunks))

    except Exception as e:
        _set_status(workspace_id, status="error", error=str(e))


def _embed_openai(chunks: list[dict], cfg: dict) -> Optional[list]:
    """Generate embeddings via OpenAI — only called if configured."""
    try:
        import requests
        import numpy as np
        texts = [f"# {c['file']} (lines {c['start_line']}-{c['end_line']})\n{c['text']}"
                 for c in chunks]
        # Batch in groups of 100
        embeddings = []
        for i in range(0, len(texts), 100):
            batch = texts[i:i + 100]
            r = requests.post(
                f"{cfg.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/embeddings",
                headers={"Authorization": f"Bearer {cfg['OPENAI_API_KEY']}"},
                json={"model": cfg.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
                      "input": batch},
                timeout=60,
            )
            r.raise_for_status()
            batch_embeddings = [item["embedding"] for item in r.json()["data"]]
            embeddings.extend(batch_embeddings)
        return embeddings
    except Exception:
        return None


def _embed_ollama(chunks: list[dict], cfg: dict) -> Optional[list]:
    """Generate embeddings via Ollama — only called if available."""
    try:
        import requests
        base_url = cfg.get("OLLAMA_BASE_URL", "http://localhost:11434")
        model = cfg.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        embeddings = []
        for c in chunks:
            text = f"# {c['file']} (lines {c['start_line']}-{c['end_line']})\n{c['text']}"
            r = requests.post(f"{base_url}/api/embeddings",
                              json={"model": model, "prompt": text}, timeout=60)
            r.raise_for_status()
            embeddings.append(r.json()["embedding"])
        return embeddings
    except Exception:
        return None


def load_index(workspace_id: int, repo_path: str, cfg=None) -> Optional[dict]:
    """Load index from cache, or None if not indexed."""
    cache_file = _cache_path(workspace_id, repo_path, cfg)
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def list_files(workspace_id: int, repo_path: str, cfg=None) -> list[dict]:
    """Return file tree for the workspace."""
    data = load_index(workspace_id, repo_path, cfg)
    if not data:
        return []
    seen = {}
    for chunk in data["chunks"]:
        f = chunk["file"]
        if f not in seen:
            seen[f] = {"path": f, "lines": chunk["end_line"]}
        else:
            seen[f]["lines"] = max(seen[f]["lines"], chunk["end_line"])
    return sorted(seen.values(), key=lambda x: x["path"])
