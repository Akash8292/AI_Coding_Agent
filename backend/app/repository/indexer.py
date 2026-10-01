"""
Repository file indexer.

Walks a repo (git-aware: respects .gitignore), chunks source files, and builds
a BM25 keyword index. Optional semantic embeddings via OpenAI or Ollama.

The index is persisted per workspace on disk and kept FRESH: every search
calls refresh_index(), which stats the tracked files and re-chunks only those
whose mtime/size changed (or that were added/removed) — so answers never use
stale code after an edit is applied.
"""
import hashlib
import logging
import math
import os
import pickle
import re
import threading
import time
from collections import Counter
from typing import Optional

from app.repository import workspace_fs

logger = logging.getLogger(__name__)

IGNORE_DIRS = workspace_fs.IGNORE_DIRS
ALLOWED_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rs", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala", ".sh",
    ".bash", ".ps1", ".sql", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue",
    ".svelte", ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".gradle", ".proto", ".graphql", ".tf", ".dart", ".lua", ".r", ".ex", ".exs",
}
ALLOWED_NAMES = {"dockerfile", "makefile", "procfile", "gemfile", "rakefile", "jenkinsfile",
                 ".env.example", "requirements.txt"}
MAX_FILE_BYTES = 500_000
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
INDEX_VERSION = 3

# Per-workspace in-progress status (not persisted)
_status_lock = threading.Lock()
_status: dict = {}  # workspace_id -> status dict

# In-memory copy of loaded indexes + per-workspace refresh locks
_mem_lock = threading.Lock()
_mem: dict[str, dict] = {}
_ws_locks: dict[str, threading.Lock] = {}


def _get_cache_dir(cfg=None) -> str:
    d = (cfg.get("INDEX_CACHE_DIR", "") if cfg else "") or \
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "index_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(workspace_id: int, repo_path: str, cfg=None) -> str:
    h = hashlib.sha256(f"{workspace_id}:{os.path.abspath(repo_path)}".encode()).hexdigest()[:16]
    return os.path.join(_get_cache_dir(cfg), f"ws_{workspace_id}_{h}.pkl")


def _lock_for(cache_file: str) -> threading.Lock:
    with _mem_lock:
        return _ws_locks.setdefault(cache_file, threading.Lock())


# ── Tokenisation ────────────────────────────────────────────────────────────

_SUFFIXES = ("izations", "ization", "ications", "ication", "ations", "ation", "ements", "ement",
             "ities", "ity", "ingly", "ings", "ing", "ated", "ates", "ate", "ions", "ion",
             "ers", "er", "ed", "es", "s")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def tokenize(text: str) -> list[str]:
    """Identifiers plus their snake/camel parts, lowercased and lightly stemmed."""
    out = []
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        low = ident.lower()
        out.append(stem(low))
        parts = [p for chunk in ident.split("_") for p in _CAMEL.findall(chunk)]
        if len(parts) > 1:
            out.extend(stem(p.lower()) for p in parts if len(p) > 2)
    return out


# ── File discovery / chunking ───────────────────────────────────────────────

def _indexable(rel: str) -> bool:
    name = os.path.basename(rel).lower()
    ext = os.path.splitext(name)[1]
    return ext in ALLOWED_EXT or name in ALLOWED_NAMES


def _iter_files(repo_path: str) -> list[str]:
    """Relative paths of indexable files (git-aware, secrets excluded)."""
    out = []
    for rel in workspace_fs.list_files(repo_path):
        if not _indexable(rel):
            continue
        try:
            if os.path.getsize(os.path.join(repo_path, rel)) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(rel)
    return out


def _chunk_file(rel: str, repo_path: str) -> list[dict]:
    if os.path.isabs(rel):
        rel = os.path.relpath(rel, repo_path).replace("\\", "/")
    try:
        text = workspace_fs.read_text(repo_path, rel, MAX_FILE_BYTES)
    except (OSError, workspace_fs.WorkspacePathError):
        return []
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    chunks = []
    step = max(CHUNK_LINES - CHUNK_OVERLAP, 1)
    for start in range(0, len(lines), step):
        end = min(start + CHUNK_LINES, len(lines))
        body = "".join(lines[start:end]).strip()
        if body:
            chunks.append({"file": rel, "start_line": start + 1, "end_line": end, "text": body})
        if end >= len(lines):
            break
    return chunks


def _file_sig(repo_path: str, rel: str) -> Optional[tuple]:
    try:
        st = os.stat(os.path.join(repo_path, rel))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _build_keyword_index(chunks: list[dict]) -> dict:
    df: Counter = Counter()
    doc_tf = []
    doc_len = []
    for chunk in chunks:
        toks = tokenize(chunk["text"]) + tokenize(chunk["file"]) * 3  # path terms weigh more
        tf = Counter(toks)
        doc_tf.append(tf)
        doc_len.append(len(toks))
        df.update(tf.keys())
    n = len(chunks)
    return {"df": dict(df), "doc_tf": doc_tf, "doc_len": doc_len, "N": n,
            "avg_dl": (sum(doc_len) / n) if n else 0.0}


def bm25_score(query_tokens: list[str], doc_tf, df: dict, N: int, avg_dl: float,
               k1: float = 1.5, b: float = 0.75, dl: Optional[int] = None) -> float:
    if isinstance(doc_tf, list):  # legacy: list of tokens
        dl = len(doc_tf)
        doc_tf = Counter(doc_tf)
    dl = dl if dl is not None else sum(doc_tf.values())
    score = 0.0
    for term in query_tokens:
        tf = doc_tf.get(term, 0)
        if not tf:
            continue
        d = df.get(term, 0)
        idf = math.log((N - d + 0.5) / (d + 0.5) + 1)
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1e-9)))
    return score


# ── Status ──────────────────────────────────────────────────────────────────

def get_status(workspace_id: int) -> dict:
    with _status_lock:
        return dict(_status.get(workspace_id, {"status": "idle"}))


def _set_status(workspace_id: int, **kwargs):
    with _status_lock:
        _status.setdefault(workspace_id, {"status": "idle"}).update(kwargs)


# ── Build / load / refresh ──────────────────────────────────────────────────

def start_indexing(workspace_id: int, repo_path: str, cfg=None,
                   on_complete=None, force: bool = False) -> str:
    """
    Start background indexing. Returns 'cached' if a usable index exists
    (it is refreshed incrementally in the background), 'started' otherwise.
    on_complete: optional callback(workspace_id, total_files, total_chunks)
    """
    repo_path = os.path.abspath(repo_path)
    if not os.path.isdir(repo_path):
        raise ValueError(f"Not a directory: {repo_path}")
    cache_file = _cache_path(workspace_id, repo_path, cfg)
    existing = None if force else load_index(workspace_id, repo_path, cfg)
    _set_status(workspace_id, status="indexing", path=repo_path, total_files=0,
                indexed_files=0, total_chunks=0, error=None)

    def work():
        try:
            if existing is not None:
                data = refresh_index(workspace_id, repo_path, cfg)
            else:
                data = _run_index(workspace_id, repo_path, cache_file, cfg)
            total_files = data.get("total_files", 0) if data else 0
            total_chunks = len(data.get("chunks", [])) if data else 0
            _set_status(workspace_id, status="done", total_files=total_files,
                        indexed_files=total_files, total_chunks=total_chunks)
            if on_complete:
                on_complete(workspace_id, total_files, total_chunks)
        except Exception as e:
            logger.exception("[index] workspace %s failed", workspace_id)
            _set_status(workspace_id, status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return "cached" if existing is not None else "started"


def _run_index(workspace_id: int, repo_path: str, cache_file: str,
               cfg=None, on_complete=None) -> dict:
    """Full (re)index. Also used synchronously as a fallback by search()."""
    with _lock_for(cache_file):
        files = _iter_files(repo_path)
        _set_status(workspace_id, total_files=len(files))
        all_chunks: list[dict] = []
        sigs = {}
        for i, rel in enumerate(files):
            all_chunks.extend(_chunk_file(rel, repo_path))
            sigs[rel] = _file_sig(repo_path, rel)
            if i % 50 == 0:
                _set_status(workspace_id, indexed_files=i + 1, total_chunks=len(all_chunks))
        data = _assemble(all_chunks, sigs, repo_path, cfg)
        _save(cache_file, data)
    if on_complete:
        on_complete(workspace_id, len(files), len(all_chunks))
    return data


def _assemble(chunks: list[dict], sigs: dict, repo_path: str, cfg) -> dict:
    embeddings = None
    provider = (cfg or {}).get("EMBEDDING_PROVIDER", "none") if cfg else "none"
    if provider == "openai" and cfg and cfg.get("OPENAI_API_KEY"):
        embeddings = _embed_openai(chunks, cfg)
    elif provider == "ollama":
        embeddings = _embed_ollama(chunks, cfg)
    return {
        "version": INDEX_VERSION,
        "chunks": chunks,
        "keyword_index": _build_keyword_index(chunks),
        "embeddings": embeddings,
        "files": sigs,
        "total_files": len(sigs),
        "repo_path": repo_path,
        "built_at": time.time(),
    }


def _save(cache_file: str, data: dict) -> None:
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, cache_file)
    with _mem_lock:
        _mem[cache_file] = {"data": data, "mtime": os.path.getmtime(cache_file)}


def load_index(workspace_id: int, repo_path: str, cfg=None) -> Optional[dict]:
    """Load the index (memory first, then disk). None if absent or outdated format."""
    cache_file = _cache_path(workspace_id, repo_path, cfg)
    if not os.path.exists(cache_file):
        return None
    try:
        mtime = os.path.getmtime(cache_file)
        with _mem_lock:
            hit = _mem.get(cache_file)
        if hit and hit["mtime"] == mtime:
            return hit["data"]
        with open(cache_file, "rb") as f:
            data = pickle.load(f)
        if data.get("version") != INDEX_VERSION:
            return None
        with _mem_lock:
            _mem[cache_file] = {"data": data, "mtime": mtime}
        return data
    except Exception:
        return None


def refresh_index(workspace_id: int, repo_path: str, cfg=None) -> Optional[dict]:
    """
    Bring the index up to date with the working tree. Cheap when nothing
    changed (one stat per file); otherwise re-chunks only changed files.
    """
    cache_file = _cache_path(workspace_id, repo_path, cfg)
    data = load_index(workspace_id, repo_path, cfg)
    if data is None:
        return _run_index(workspace_id, repo_path, cache_file, cfg)
    with _lock_for(cache_file):
        data = load_index(workspace_id, repo_path, cfg) or data
        current = {rel: _file_sig(repo_path, rel) for rel in _iter_files(repo_path)}
        old = data.get("files", {})
        changed = {rel for rel, sig in current.items() if old.get(rel) != sig}
        removed = set(old) - set(current)
        if not changed and not removed:
            return data
        keep = [c for c in data["chunks"] if c["file"] not in changed and c["file"] not in removed]
        for rel in sorted(changed):
            keep.extend(_chunk_file(rel, repo_path))
        keep.sort(key=lambda c: (c["file"], c["start_line"]))
        cfg_no_embed = dict(cfg or {})
        cfg_no_embed["EMBEDDING_PROVIDER"] = "none"  # embeddings rebuilt only on full re-index
        new = _assemble(keep, current, repo_path, cfg_no_embed)
        _save(cache_file, new)
        logger.info("[index] ws=%s refreshed: %d changed, %d removed", workspace_id,
                    len(changed), len(removed))
        return new


def _embed_openai(chunks: list[dict], cfg: dict) -> Optional[list]:
    """Generate embeddings via OpenAI — only called if configured."""
    try:
        import requests
        texts = [f"# {c['file']} (lines {c['start_line']}-{c['end_line']})\n{c['text']}" for c in chunks]
        embeddings = []
        for i in range(0, len(texts), 100):
            r = requests.post(
                f"{cfg.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/embeddings",
                headers={"Authorization": f"Bearer {cfg['OPENAI_API_KEY']}"},
                json={"model": cfg.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
                      "input": texts[i:i + 100]},
                timeout=(10, 60),
            )
            r.raise_for_status()
            embeddings.extend(item["embedding"] for item in r.json()["data"])
        return embeddings
    except Exception as e:
        logger.warning("[index] OpenAI embeddings failed: %s", e)
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
                              json={"model": model, "prompt": text}, timeout=(3, 60))
            r.raise_for_status()
            embeddings.append(r.json()["embedding"])
        return embeddings
    except Exception as e:
        logger.warning("[index] Ollama embeddings failed: %s", e)
        return None


def list_files(workspace_id: int, repo_path: str, cfg=None) -> list[dict]:
    """File tree for the workspace: every file (not only indexed ones) with line counts."""
    data = load_index(workspace_id, repo_path, cfg)
    lines: dict[str, int] = {}
    if data:
        for chunk in data["chunks"]:
            lines[chunk["file"]] = max(lines.get(chunk["file"], 0), chunk["end_line"])
    files = workspace_fs.list_files(repo_path) if os.path.isdir(repo_path) else []
    return [{"path": f, "lines": lines.get(f)} for f in files]
