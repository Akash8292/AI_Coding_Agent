import json


def test_register_success(client):
    res = client.post("/auth/register", json={
        "email": "newuser@example.com",
        "password": "validpassword123",
        "name": "New User"
    })
    assert res.status_code == 201
    data = res.get_json()
    assert "token" in data
    assert data["user"]["email"] == "newuser@example.com"


def test_register_invalid_email(client):
    res = client.post("/auth/register", json={
        "email": "invalid-email",
        "password": "validpassword123"
    })
    assert res.status_code == 400


def test_register_short_password(client):
    res = client.post("/auth/register", json={
        "email": "short@example.com",
        "password": "123"
    })
    assert res.status_code == 400


def test_register_duplicate(client, test_user):
    res = client.post("/auth/register", json={
        "email": test_user.email,
        "password": "anotherpassword123"
    })
    assert res.status_code == 409


def test_login_success(client, test_user):
    res = client.post("/auth/login", json={
        "email": test_user.email,
        "password": "securepassword123"
    })
    assert res.status_code == 200
    data = res.get_json()
    assert "token" in data
    assert data["user"]["id"] == test_user.id


def test_login_invalid_password(client, test_user):
    res = client.post("/auth/login", json={
        "email": test_user.email,
        "password": "wrongpassword"
    })
    assert res.status_code == 401


def test_me_authorized(client, auth_headers):
    res = client.get("/auth/me", headers=auth_headers)
    assert res.status_code == 200
    data = res.get_json()
    assert "user" in data
    assert data["user"]["email"] == "dev@example.com"


def test_me_unauthorized(client):
    res = client.get("/auth/me")
    assert res.status_code == 401
