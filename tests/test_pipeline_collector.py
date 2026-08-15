"""Tests for the pipeline data layer (pipeline_data.py)."""

import pytest
from unittest.mock import patch, MagicMock

from ai_stock.dataflows.pipeline_data import (
    classify_news,
    title_hash,
    get_impact_news,
    get_limit_up_stocks,
)


@pytest.mark.unit
class TestClassifyNews:
    def test_policy_classification(self):
        assert classify_news("国务院发布新政策", "关于降准降息的通知") == "policy"

    def test_news_classification(self):
        assert classify_news("某公司发布新产品", "产品功能介绍") == "news"

    def test_single_keyword_is_news(self):
        # Only 1 policy keyword → news (threshold is >=2)
        assert classify_news("央行行长讲话", "常规发言") == "news"

    def test_multiple_keywords_is_policy(self):
        assert classify_news("央行降准 证监会通知", "关于金融支持政策") == "policy"


@pytest.mark.unit
class TestTitleHash:
    def test_deterministic(self):
        assert title_hash("test") == title_hash("test")

    def test_different_titles(self):
        assert title_hash("aaa") != title_hash("bbb")

    def test_length(self):
        assert len(title_hash("test")) == 12


@pytest.mark.unit
class TestGetImpactNews:
    @patch("ai_stock.dataflows.pipeline_data._requests")
    @patch("ai_stock.dataflows.pipeline_data._em_get")
    def test_returns_list(self, mock_em, mock_req):
        # Mock CLS
        cls_resp = MagicMock()
        cls_resp.json.return_value = {
            "data": {"roll_data": [
                {"title": "测试新闻", "content": "内容", "ctime": ""},
            ]}
        }
        mock_req.get.return_value = cls_resp

        # Mock Eastmoney
        em_resp = MagicMock()
        em_resp.json.return_value = {"data": {"fastNewsList": []}}
        mock_em.return_value = em_resp

        # Mock Baidu
        baidu_resp = MagicMock()
        baidu_resp.json.return_value = {"data": {"data": []}}
        # requests.get is called for CLS and Baidu
        mock_req.get.side_effect = [cls_resp, baidu_resp]

        result = get_impact_news("2025-01-15", hours=12)
        assert isinstance(result, list)
        # At least the CLS item should be present
        assert len(result) >= 1
        assert result[0]["title"] == "测试新闻"
        assert "title_hash" in result[0]
        assert "category" in result[0]


@pytest.mark.unit
class TestGetLimitUpStocks:
    @patch("ai_stock.dataflows.pipeline_data._requests")
    @patch("ai_stock.dataflows.pipeline_data._time")
    def test_returns_list(self, mock_time, mock_req):
        resp = MagicMock()
        resp.json.return_value = {
            "errocode": 0,
            "data": [
                {"code": "000001", "name": "测试股", "reason": "概念A+概念B",
                 "zhangfu": "10.02", "huanshou": "5.2", "chengjiaoe": "10亿",
                 "ddejingliang": "1亿"},
            ],
        }
        mock_req.get.return_value = resp

        result = get_limit_up_stocks("2025-01-15", days=1)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0]["code"] == "000001"
        assert result[0]["reason_tags"] == ["概念A", "概念B"]
