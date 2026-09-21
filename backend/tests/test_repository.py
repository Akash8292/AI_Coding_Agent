import os
from app.repository.indexer import _chunk_file, _build_keyword_index, bm25_score, _run_index, _cache_path, load_index, list_files
from app.repository.searcher import search, read_file


def test_chunk_file(temp_repo):
    calc_path = os.path.join(temp_repo, "calculator.py")
    chunks = _chunk_file(calc_path, temp_repo)
    assert len(chunks) >= 1
    assert "add" in chunks[0]["text"]
    assert chunks[0]["file"] == "calculator.py"


def test_indexing_and_search(temp_repo, app):
    with app.app_context():
        cache_file = _cache_path(1, temp_repo, app.config)
        _run_index(workspace_id=1, repo_path=temp_repo, cache_file=cache_file, cfg=app.config)

        # Verify index was written and can be loaded
        assert os.path.exists(cache_file)

        # Test list_files
        files = list_files(1, temp_repo, cfg=app.config)
        assert len(files) >= 1
        assert any(f["path"] == "calculator.py" for f in files)

        # Test search
        results = search(workspace_id=1, repo_path=temp_repo, query="add", top_k=5, cfg=app.config)
        assert len(results) > 0
        assert any("calculator.py" in r["file"] for r in results)


def test_read_file(temp_repo):
    content = read_file(temp_repo, "calculator.py")
    assert content is not None
    assert "def add(a, b):" in content

    # Test reading non-existent file
    assert read_file(temp_repo, "non_existent.py") is None

    # Test security against path traversal
    assert read_file(temp_repo, "../../../etc/passwd") is None
