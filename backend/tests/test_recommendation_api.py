"""Tests for the recommendation API surface.

- The manual-trigger endpoint was removed: rankings update exclusively on
  the server-side schedule (see ai_stock.pipeline.config.PIPELINE_SCHEDULE).
- History queries return empty data (not 404) when nothing exists for the
  requested date, so pages can render an empty state directly.
"""

from __future__ import annotations

from app.api import recommendation as recommendation_api


class _FakeSvc:
    """Stands in for the pipeline service; no DB or engine needed."""

    def get_by_date(self, date: str):
        return None


def test_trigger_endpoint_removed(client, auth_headers):
    resp = client.post("/api/recommendation/trigger", headers=auth_headers)
    assert resp.status_code == 404


def test_history_returns_empty_when_no_data(client, auth_headers, monkeypatch):
    monkeypatch.setattr(recommendation_api, "get_pipeline_service", _FakeSvc)
    resp = client.get(
        "/api/recommendation/history",
        params={"date": "2030-01-01"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"snapshot": None, "recommendations": []}
