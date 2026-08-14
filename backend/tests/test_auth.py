"""Auth API tests (Phase 1 acceptance criteria)."""

from __future__ import annotations


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_register_login_me(client):
    # Register
    resp = client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "strong-pass-123",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert "password" not in body and "password_hash" not in body

    # Login
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert token

    # Me
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "alice@example.com"


def test_register_duplicate_username(client):
    payload = {
        "username": "bob",
        "email": "bob@example.com",
        "password": "strong-pass-123",
    }
    assert client.post("/api/auth/register", json=payload).status_code == 201
    resp = client.post(
        "/api/auth/register",
        json={**payload, "email": "bob2@example.com"},
    )
    assert resp.status_code == 409


def test_register_short_password(client):
    resp = client.post(
        "/api/auth/register",
        json={"username": "carl", "email": "carl@example.com", "password": "short"},
    )
    assert resp.status_code == 422


def test_login_wrong_password(client):
    client.post(
        "/api/auth/register",
        json={"username": "dave", "email": "dave@example.com", "password": "strong-pass-123"},
    )
    resp = client.post(
        "/api/auth/login", json={"username": "dave", "password": "wrong-password"}
    )
    assert resp.status_code == 401


def test_protected_route_without_token(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_protected_route_invalid_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_watchlist_crud(client, auth_headers):
    # List empty
    assert client.get("/api/watchlist", headers=auth_headers).json() == []

    # Add invalid ticker → 400 (engine resolution rejects)
    resp = client.post(
        "/api/watchlist", headers=auth_headers, json={"ticker": "不是股票"}
    )
    assert resp.status_code == 400

    # Add a valid-format code (resolution depends on local market data;
    # 600519 is a main-board code that should resolve offline)
    resp = client.post(
        "/api/watchlist", headers=auth_headers, json={"ticker": "600519"}
    )
    assert resp.status_code in (201, 400)  # 400 if data files unavailable
    if resp.status_code == 201:
        code = resp.json()["ticker"]
        assert code == "600519"
        # Duplicate → 409
        assert (
            client.post(
                "/api/watchlist", headers=auth_headers, json={"ticker": "600519"}
            ).status_code
            == 409
        )
        # Remove
        assert (
            client.delete(f"/api/watchlist/{code}", headers=auth_headers).status_code
            == 200
        )
