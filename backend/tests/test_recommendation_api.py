"""Tests for the recommendation (热股榜) API surface.

- 热股榜 = 自选池活跃标的 (quant 选股流程产出); 无手动触发端点。
- History queries return empty data (not 404) when nothing exists for the
  requested date, so pages can render an empty state directly.
"""

from __future__ import annotations

from app.api import recommendation as recommendation_api


class _FakeSvc:
    """Stands in for the pipeline service; no DB or engine needed."""

    def get_hot_stocks_by_date(self, date: str):
        return {"snapshot": {"source": "quant_optional_pool", "as_of": date},
                "recommendations": []}


def test_trigger_endpoint_removed(client, auth_headers):
    resp = client.post("/api/recommendation/trigger", headers=auth_headers)
    assert resp.status_code == 404


def test_history_returns_empty_when_no_data(client, auth_headers, monkeypatch):
    monkeypatch.setattr(recommendation_api, "get_pipeline_service", lambda: _FakeSvc())
    resp = client.get(
        "/api/recommendation/history",
        params={"date": "2030-01-01"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommendations"] == []
    assert body["snapshot"]["as_of"] == "2030-01-01"
