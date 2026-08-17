"""Shared fixtures: isolated SQLite DB + TestClient."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Isolate tests from any developer .env before app modules import settings.
import os

BACKEND_DIR = Path(__file__).resolve().parent.parent
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-0123456789abcdef0123456789abcdef")
os.environ["DATABASE_URL"] = "sqlite://"  # in-memory
# Keep TestClient lifespan from starting real pipeline/LLM/scheduler threads
# (the in-memory DB would otherwise trigger a bootstrap run on every suite).
os.environ["AISTOCK_DISABLE_SCHEDULERS"] = "1"

from app.core.database import Base, get_db  # noqa: E402
import app.models  # noqa: F401, E402

from app.main import app  # noqa: E402

_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # one shared in-memory DB across threads
)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db_session():
    Base.metadata.create_all(bind=_test_engine)
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_test_engine)


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_headers(client):
    """Register + login a user, return Authorization headers."""
    resp = client.post(
        "/api/auth/register",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "password": "secret-password",
        },
    )
    assert resp.status_code == 201, resp.text
    token = resp.json().get("access_token")
    if not token:
        login = client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "secret-password"},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
