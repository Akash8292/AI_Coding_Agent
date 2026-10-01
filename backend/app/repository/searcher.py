"""
Repository search — two-tier:
1. BM25 keyword search over identifiers (always available, zero dependencies)
2. Semantic cosine similarity (optional, when embeddings exist)

The index is refreshed incrementally before each search, so results always
reflect the current working tree.
"""
import logging
import os
from typing import Optional

from app.repository import workspace_fs
from app.repository.indexer import bm25_score, refresh_index, tokenize

logger = logging.getLogger(__name__)

QUERY_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
                   "are", "how", "what", "why", "where", "does", "do", "this", "that", "it",
                   "work", "works", "explain", "show", "me", "project", "code", "file", "can",
                   "you", "please", "which", "tell", "about", "use", "we", "our", "my"}


def search(workspace_id: int, repo_path: str, query: str,
           top_k: int = 8, cfg=None) -> list[dict]:
    """
    Search the repo for chunks relevant to the query.
    Returns list of {file, start_line, end_line, text, score, search_type}.
    """
    data = refresh_index(workspace_id, repo_path, cfg)
    if not data or not data.get("chunks"):
        return []

    chunks = data["chunks"]
    ki = data.get("keyword_index") or {}
    embeddings = data.get("embeddings")

    # --- BM25 keyword search -------------------------------------------------
    q_tokens = [t for t in dict.fromkeys(tokenize(query)) if t not in QUERY_STOPWORDS]
    bm25_results = []
    if ki and q_tokens:
        weighted = expand_terms(q_tokens, ki["df"])
        for i, chunk in enumerate(chunks):
            score = sum(w * bm25_score([term], ki["doc_tf"][i], ki["df"], ki["N"], ki["avg_dl"],
                                       dl=ki["doc_len"][i])
                        for term, w in weighted)
            if score > 0:
                bm25_results.append({**chunk, "score": score, "search_type": "keyword"})
        bm25_results.sort(key=lambda x: x["score"], reverse=True)
        bm25_results = bm25_results[:top_k]
        if bm25_results and bm25_results[0]["score"] > 0:
            top = bm25_results[0]["score"]
            for r in bm25_results:
                r["raw_score"] = r["score"]
                r["score"] = r["score"] / top

    # --- Semantic search (if embeddings available) ---------------------------
    semantic_results = []
    if embeddings and len(embeddings) == len(chunks):
        semantic_results = _semantic_search(query, chunks, embeddings, top_k, cfg) or []

    if not bm25_results and not semantic_results:
        q_lower = query.lower().strip()
        if q_lower:
            for chunk in chunks:
                if q_lower in chunk["text"].lower() or q_lower in chunk["file"].lower():
                    bm25_results.append({**chunk, "score": 0.5, "search_type": "substring"})
            bm25_results = bm25_results[:top_k]

    if semantic_results and bm25_results:
        seen, merged = set(), []
        for r in semantic_results + [{**r, "score": r["score"] * 0.8} for r in bm25_results]:
            key = (r["file"], r["start_line"])
            if key not in seen:
                seen.add(key)
                merged.append(r)
        return sorted(merged, key=lambda x: x["score"], reverse=True)[:top_k]

    return (semantic_results or bm25_results)[:top_k]


def expand_terms(q_tokens: list[str], df: dict, max_per_term: int = 6) -> list[tuple[str, float]]:
    """
    Exact query terms (weight 1.0) plus index terms sharing a >=4-char prefix
    relationship (weight 0.6): "authentication"→"auth", "config"→"configuration".
    """
    out: dict[str, float] = {}
    for q in q_tokens:
        if q in df:
            out[q] = max(out.get(q, 0), 1.0)
        if len(q) < 4:
            continue
        related = [t for t in df if t != q and len(t) >= 4 and (q.startswith(t) or t.startswith(q))]
        related.sort(key=lambda t: -df[t])
        for t in related[:max_per_term]:
            out[t] = max(out.get(t, 0), 0.6)
    return list(out.items())


def vocabulary_count(workspace_id: int, repo_path: str, cfg=None):
    """Callable term -> number of chunks containing it (for scope detection)."""
    from app.repository.indexer import load_index, stem
    data = load_index(workspace_id, repo_path, cfg)
    df = (data or {}).get("keyword_index", {}).get("df", {}) if data else {}

    def count(term: str) -> int:
        return df.get(stem(term.lower()), 0)
    return count


def _semantic_search(query: str, chunks: list[dict], embeddings: list,
                     top_k: int, cfg=None) -> Optional[list[dict]]:
    """Cosine similarity search using pre-computed embeddings."""
    try:
        import numpy as np
        q_vec = _embed_query(query, cfg)
        if q_vec is None:
            return None
        emb_array = np.array(embeddings, dtype=np.float32)
        q_arr = np.array(q_vec, dtype=np.float32)
        q_norm = q_arr / (np.linalg.norm(q_arr) + 1e-8)
        m_norm = emb_array / (np.linalg.norm(emb_array, axis=1, keepdims=True) + 1e-8)
        sims = m_norm @ q_norm
        top_idx = sims.argsort()[::-1][:top_k]
        return [{**chunks[i], "score": float(sims[i]), "search_type": "semantic"}
                for i in top_idx if sims[i] > 0.1]
    except Exception:
        return None


def _embed_query(query: str, cfg=None) -> Optional[list]:
    if not cfg:
        return None
    provider = cfg.get("EMBEDDING_PROVIDER", "none")
    try:
        import requests
        if provider == "openai" and cfg.get("OPENAI_API_KEY"):
            r = requests.post(
                f"{cfg.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/embeddings",
                headers={"Authorization": f"Bearer {cfg['OPENAI_API_KEY']}"},
                json={"model": cfg.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"), "input": query},
                timeout=(5, 15),
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
        if provider == "ollama":
            r = requests.post(f"{cfg.get('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/embeddings",
                              json={"model": cfg.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
                                    "prompt": query}, timeout=(2, 15))
            r.raise_for_status()
            return r.json()["embedding"]
    except Exception:
        return None
    return None


def read_file(repo_path: str, rel_path: str) -> Optional[str]:
    """Safely read a text file from within the repo (None if unreadable or protected)."""
    try:
        return workspace_fs.read_text(repo_path, rel_path)
    except (OSError, workspace_fs.WorkspacePathError):
        return None
