import json


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] in ("healthy", "degraded")
    assert data["database"] == "connected"
    assert "providers_available" in data


def test_models_endpoint(client):
    res = client.get("/api/models")
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, dict)
    assert "openai" in data


def test_workspaces_crud(client, auth_headers, temp_repo):
    # Create workspace
    create_res = client.post("/api/workspaces", headers=auth_headers, json={
        "name": "Test Workspace",
        "repo_path": temp_repo
    })
    assert create_res.status_code == 201
    ws = create_res.get_json()
    ws_id = ws["id"]
    assert ws["name"] == "Test Workspace"

    # List workspaces
    list_res = client.get("/api/workspaces", headers=auth_headers)
    assert list_res.status_code == 200
    workspaces = list_res.get_json()["workspaces"]
    assert any(w["id"] == ws_id for w in workspaces)

    # Get single workspace
    get_res = client.get(f"/api/workspaces/{ws_id}", headers=auth_headers)
    assert get_res.status_code == 200
    assert get_res.get_json()["id"] == ws_id

    # Delete workspace
    del_res = client.delete(f"/api/workspaces/{ws_id}", headers=auth_headers)
    assert del_res.status_code == 200
    assert del_res.get_json()["ok"] is True


def test_conversations_crud(client, auth_headers):
    # Create conversation
    create_res = client.post("/api/conversations", headers=auth_headers, json={
        "title": "Architecture Discussion",
        "provider": "openai",
        "model": "gpt-4o"
    })
    assert create_res.status_code == 201
    conv = create_res.get_json()
    conv_id = conv["id"]
    assert conv["title"] == "Architecture Discussion"

    # List conversations
    list_res = client.get("/api/conversations", headers=auth_headers)
    assert list_res.status_code == 200
    conversations = list_res.get_json()["conversations"]
    assert any(c["id"] == conv_id for c in conversations)

    # Update conversation title
    update_res = client.put(f"/api/conversations/{conv_id}", headers=auth_headers, json={
        "title": "Renamed Discussion"
    })
    assert update_res.status_code == 200
    assert update_res.get_json()["title"] == "Renamed Discussion"

    # Delete conversation
    del_res = client.delete(f"/api/conversations/{conv_id}", headers=auth_headers)
    assert del_res.status_code == 200


def test_usage_endpoint(client, auth_headers):
    res = client.get("/api/usage?period=all", headers=auth_headers)
    assert res.status_code == 200
    data = res.get_json()
    assert "requests" in data
    assert "total_tokens" in data
    assert "cost_usd" in data


def test_chat_cancel_missing_req_id(client, auth_headers):
    res = client.post("/api/chat/cancel", headers=auth_headers, json={})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_chat_cancel_not_found(client, auth_headers):
    res = client.post("/api/chat/cancel", headers=auth_headers, json={"request_id": "nonexistent_req_123"})
    assert res.status_code == 404
    assert res.get_json()["ok"] is False


def test_chat_cancel_active_request(client, auth_headers):
    from app.api.chat import _register_request, _unregister_request, _is_cancelled
    ev = _register_request("test_req_active_456")
    try:
        assert not _is_cancelled(ev)
        res = client.post("/api/chat/cancel", headers=auth_headers, json={"request_id": "test_req_active_456"})
        assert res.status_code == 200
        assert res.get_json()["ok"] is True
        assert _is_cancelled(ev)
    finally:
        _unregister_request("test_req_active_456")

