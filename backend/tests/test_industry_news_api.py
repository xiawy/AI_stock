"""Tests for the industry-news API surface (行业榜 → 对应新闻).

- GET /api/industry/{ranking_id}/news returns the news items that fed the
  ranking row's heat score (stubbed service, no DB needed).
- Unknown ranking ids surface as 404.
"""

from __future__ import annotations

from app.api import industry as industry_api


class _FakeSvc:
    """Stands in for the pipeline service; no DB or engine needed."""

    def __init__(self, payload=None):
        self._payload = payload
        self.calls = []

    def get_industry_news(self, ranking_id: int):
        self.calls.append(ranking_id)
        return self._payload


def test_industry_news_ok(client, auth_headers, monkeypatch):
    payload = {
        "industry": "白酒",
        "snapshot_id": 5,
        "news_items": [
            {
                "id": 1,
                "title": "白酒板块提价",
                "composite_score": 7.5,
                "bull_bear_bias": "bullish",
            },
            {
                "id": 2,
                "title": "消费数据回暖",
                "composite_score": 6.2,
                "bull_bear_bias": "neutral",
            },
        ],
    }
    fake = _FakeSvc(payload)
    monkeypatch.setattr(industry_api, "get_pipeline_service", lambda: fake)

    resp = client.get("/api/industry/3/news", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["industry"] == "白酒"
    assert [n["id"] for n in body["news_items"]] == [1, 2]
    assert fake.calls == [3]


def test_industry_news_not_found(client, auth_headers, monkeypatch):
    monkeypatch.setattr(
        industry_api, "get_pipeline_service", lambda: _FakeSvc(None),
    )
    resp = client.get("/api/industry/999/news", headers=auth_headers)
    assert resp.status_code == 404
