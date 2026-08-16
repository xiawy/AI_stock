"""Per-user isolation of diagnosis records (history / reports / tasks).

Report files on disk are shared (keyed by ticker+date only), so the API layer
must scope them to the requesting user's ``analysis_tasks`` rows. These tests
mock the heavy engine layer (``_web``) so they run without the analysis stack.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models import AnalysisTask
from app.services.analysis_service import task_manager


def _register_and_login(client, username):
    resp = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "strong-pass-123",
        },
    )
    assert resp.status_code == 201, resp.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "strong-pass-123"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


def _add_task(db_session, task_id, user_id, ticker, trade_date, status="completed"):
    db_session.add(
        AnalysisTask(
            id=task_id,
            user_id=user_id,
            ticker=ticker,
            trade_date=trade_date,
            status=status,
        )
    )
    db_session.commit()


def _fake_web(monkeypatch, entries):
    """Replace the lazy engine import with a lightweight in-memory stub."""
    web = SimpleNamespace(
        history=SimpleNamespace(
            get_history=lambda: entries,
            load_analysis=lambda path: {"trader_investment_decision": "买入"},
            extract_signal=lambda state: "Buy",
        ),
        display=SimpleNamespace(
            normalize_report_state_mentions=lambda state, ticker: None,
            stock_display_label=lambda ticker, state: f"{ticker} 名称",
        ),
        pdf=SimpleNamespace(
            generate_markdown=lambda state, ticker, date, signal: f"# {ticker} report",
            generate_pdf=lambda state, ticker, date, signal: b"%PDF-fake",
        ),
    )
    monkeypatch.setattr("app.api.history._web", lambda: web)


# Shared fake on-disk records: alice's 600519 + bob's 000001.
ENTRIES = [
    {"ticker": "600519", "date": "2026-01-05", "path": "/tmp/600519.json"},
    {"ticker": "000001", "date": "2026-01-06", "path": "/tmp/000001.json"},
]


def _seed_users(client, db_session):
    alice_id, alice_headers = _register_and_login(client, "alice")
    bob_id, bob_headers = _register_and_login(client, "bob")
    _add_task(db_session, "task-alice", alice_id, "600519", "2026-01-05")
    _add_task(db_session, "task-bob", bob_id, "000001", "2026-01-06")
    return (alice_id, alice_headers), (bob_id, bob_headers)


def test_history_list_isolation(client, db_session, monkeypatch):
    (alice_id, alice_headers), (bob_id, bob_headers) = _seed_users(client, db_session)
    _fake_web(monkeypatch, ENTRIES)

    tickers_alice = [e["ticker"] for e in client.get("/api/history", headers=alice_headers).json()]
    tickers_bob = [e["ticker"] for e in client.get("/api/history", headers=bob_headers).json()]
    assert tickers_alice == ["600519"]
    assert tickers_bob == ["000001"]

    # A user with no tasks sees nothing even though files exist on disk.
    _, carol_headers = _register_and_login(client, "carol")
    assert client.get("/api/history", headers=carol_headers).json() == []


def test_report_access_isolation(client, db_session, monkeypatch):
    (alice_id, alice_headers), (bob_id, bob_headers) = _seed_users(client, db_session)
    _fake_web(monkeypatch, ENTRIES)

    # Alice cannot read bob's report (404 — existence not leaked).
    resp = client.get("/api/history/000001/2026-01-06", headers=alice_headers)
    assert resp.status_code == 404

    # ...nor export it.
    assert (
        client.get(
            "/api/history/000001/2026-01-06/markdown", headers=alice_headers
        ).status_code
        == 404
    )
    assert (
        client.get("/api/history/000001/2026-01-06/pdf", headers=alice_headers).status_code
        == 404
    )

    # Alice's own report works end-to-end through the stub.
    resp = client.get("/api/history/600519/2026-01-05", headers=alice_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "600519"
    assert body["signal"] == "Buy"

    markdown = client.get(
        "/api/history/600519/2026-01-05/markdown", headers=alice_headers
    )
    assert markdown.status_code == 200
    assert "600519" in markdown.text

    pdf = client.get("/api/history/600519/2026-01-05/pdf", headers=alice_headers)
    assert pdf.status_code == 200


def test_task_operation_isolation(client, db_session, monkeypatch):
    (alice_id, alice_headers), (bob_id, bob_headers) = _seed_users(client, db_session)

    # Bob cannot poll / control alice's task (404 before touching the engine).
    for method, url in (
        ("get", "/api/analysis/status/task-alice"),
        ("get", "/api/analysis/result/task-alice"),
        ("post", "/api/analysis/task-alice/pause"),
        ("post", "/api/analysis/task-alice/resume"),
        ("post", "/api/analysis/task-alice/stop"),
    ):
        resp = getattr(client, method)(url, headers=bob_headers)
        assert resp.status_code == 404, (method, url, resp.text)

    # Unknown task ids are 404 for the owner too.
    assert (
        client.get("/api/analysis/status/no-such-task", headers=alice_headers).status_code
        == 404
    )

    # Alice's own status works (snapshot stubbed to avoid the heavy engine).
    monkeypatch.setattr(
        task_manager,
        "snapshot",
        lambda task_id: {
            "task_id": task_id,
            "ticker": "600519",
            "trade_date": "2026-01-05",
            "is_running": False,
            "is_complete": True,
            "is_paused": False,
            "stop_requested": False,
        },
    )
    resp = client.get("/api/analysis/status/task-alice", headers=alice_headers)
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-alice"


def test_incomplete_tasks_isolation(client, db_session, monkeypatch):
    (alice_id, alice_headers), (bob_id, bob_headers) = _seed_users(client, db_session)
    _fake_web(monkeypatch, ENTRIES)

    # alice paused 600519 (resumable); her completed 000001 task is not.
    _add_task(db_session, "task-alice-paused", alice_id, "600519", "2026-01-05", "paused")
    _add_task(db_session, "task-bob-running", bob_id, "000001", "2026-01-06", "running")

    # File-system checkpoint index is global (ticker+date keyed).
    monkeypatch.setattr(
        task_manager,
        "incomplete_tasks",
        lambda: [
            {"ticker": "600519", "trade_date": "2026-01-05", "status": "paused"},
            {"ticker": "000001", "trade_date": "2026-01-06", "status": "running"},
        ],
    )

    alice_entries = client.get("/api/analysis/incomplete", headers=alice_headers).json()
    assert [e["ticker"] for e in alice_entries] == ["600519"]

    bob_entries = client.get("/api/analysis/incomplete", headers=bob_headers).json()
    assert [e["ticker"] for e in bob_entries] == ["000001"]

    # A user without matching tasks sees no resumable entries.
    _, carol_headers = _register_and_login(client, "carol")
    assert client.get("/api/analysis/incomplete", headers=carol_headers).json() == []
