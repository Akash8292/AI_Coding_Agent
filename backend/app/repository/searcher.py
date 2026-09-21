"""
Repository search — two-tier:
1. BM25 keyword search (always available, zero dependencies)
2. Semantic cosine similarity (optional, when embeddings exist)

Results are merged and deduplicated.
"""
import re
from typing import Optional
from app.repository.indexer import load_index, bm25_score


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())


def search(workspace_id: int, repo_path: str, query: str,
           top_k: int = 8, cfg=None) -> list[dict]:
    """
    Search the indexed repo for chunks relevant to the query.
    Returns list of {file, start_line, end_line, text, score, search_type}.
    """
    data = load_index(workspace_id, repo_path, cfg)
    if not data or not data.get("chunks"):
        return []

    chunks = data["chunks"]
    keyword_index = data.get("keyword_index")
    embeddings = data.get("embeddings")

    # --- BM25 keyword search -------------------------------------------------
    bm25_results = []
    if keyword_index:
        query_tokens = tokenize(query)
        df = keyword_index["df"]
        doc_tokens = keyword_index["doc_tokens"]
        N = keyword_index["N"]
        avg_dl = keyword_index["avg_dl"]

        for i, chunk in enumerate(chunks):
            score = bm25_score(query_tokens, doc_tokens[i], df, N, avg_dl)
            if score > 0:
                bm25_results.append({**chunk, "score": score, "search_type": "keyword"})

        bm25_results.sort(key=lambda x: x["score"], reverse=True)
        bm25_results = bm25_results[:top_k]

        # Normalize scores to 0-1
        if bm25_results:
            max_score = bm25_results[0]["score"]
            if max_score > 0:
                for r in bm25_results:
                    r["score"] = r["score"] / max_score

    # --- Semantic search (if embeddings available) ---------------------------
    semantic_results = []
    if embeddings and len(embeddings) == len(chunks):
        sem = _semantic_search(query, chunks, embeddings, top_k, cfg)
        if sem:
            semantic_results = sem

    # --- Merge results -------------------------------------------------------
    if not bm25_results and not semantic_results:
        # Last resort: simple substring match
        q_lower = query.lower()
        for chunk in chunks:
            if q_lower in chunk["text"].lower() or q_lower in chunk["file"].lower():
                bm25_results.append({**chunk, "score": 0.5, "search_type": "substring"})
        bm25_results = bm25_results[:top_k]

    # Merge: prefer semantic if available, else keyword
    if semantic_results and bm25_results:
        seen_keys = set()
        merged = []
        for r in semantic_results:
            key = (r["file"], r["start_line"])
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(r)
        for r in bm25_results:
            key = (r["file"], r["start_line"])
            if key not in seen_keys:
                seen_keys.add(key)
                r["score"] = r["score"] * 0.8  # slightly downweight keyword-only
                merged.append(r)
        return sorted(merged, key=lambda x: x["score"], reverse=True)[:top_k]

    return (semantic_results or bm25_results)[:top_k]


def _semantic_search(query: str, chunks: list[dict], embeddings: list,
                     top_k: int, cfg=None) -> Optional[list[dict]]:
    """Cosine similarity search using pre-computed embeddings."""
    try:
        import numpy as np

        # Get query embedding
        q_vec = _embed_query(query, cfg)
        if q_vec is None:
            return None

        emb_array = np.array(embeddings, dtype=np.float32)
        q_arr = np.array(q_vec, dtype=np.float32)

        # Normalize
        q_norm = q_arr / (np.linalg.norm(q_arr) + 1e-8)
        m_norm = emb_array / (np.linalg.norm(emb_array, axis=1, keepdims=True) + 1e-8)
        sims = m_norm @ q_norm

        top_idx = sims.argsort()[::-1][:top_k]
        return [
            {**chunks[i], "score": float(sims[i]), "search_type": "semantic"}
            for i in top_idx if sims[i] > 0.1
        ]
    except Exception:
        return None


def _embed_query(query: str, cfg=None) -> Optional[list]:
    """Get embedding for a query string."""
    if not cfg:
        return None
    provider = cfg.get("EMBEDDING_PROVIDER", "none")

    if provider == "openai" and cfg.get("OPENAI_API_KEY"):
        try:
            import requests
            r = requests.post(
                f"{cfg.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/embeddings",
                headers={"Authorization": f"Bearer {cfg['OPENAI_API_KEY']}"},
                json={"model": cfg.get("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
                      "input": query},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
        except Exception:
            return None

    elif provider == "ollama":
        try:
            import requests
            base_url = cfg.get("OLLAMA_BASE_URL", "http://localhost:11434")
            model = cfg.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
            r = requests.post(f"{base_url}/api/embeddings",
                              json={"model": model, "prompt": query}, timeout=15)
            r.raise_for_status()
            return r.json()["embedding"]
        except Exception:
            return None

    return None


def read_file(repo_path: str, rel_path: str) -> Optional[str]:
    """Safely read a file from within the repo."""
    abs_path = os.path.realpath(os.path.join(repo_path, rel_path))
    repo_real = os.path.realpath(repo_path)
    if not abs_path.startswith(repo_real):
        return None
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


import os
