"""新闻新鲜度的时区一致性回归测试 (FR-11).

``_freshness`` 原来用 ``datetime.now()`` (主机本地时间) 去减数据源给的**北京
时间**字符串。服务器跑 UTC 时, 一条 3 小时前的消息会被算成 11 小时前, 新鲜度
权重、榜单排序、重要性加权一起漂 —— 而且漂得"看不太出来", 只是排序变差。

修复后统一用 ``calendar_utils.market_now()`` (UTC+8) 作为唯一时钟源。
"""

from datetime import datetime, timedelta, timezone

import pytest


TZ8 = timezone(timedelta(hours=8))
FROZEN = datetime(2026, 10, 21, 12, 0, tzinfo=TZ8)


@pytest.fixture()
def frozen_market_clock(monkeypatch):
    from ai_stock.quant import calendar_utils

    monkeypatch.setattr(calendar_utils, "market_now", lambda: FROZEN)
    return FROZEN


def _freshness(pub_time: str) -> float:
    from ai_stock.pipeline.news_impact import _freshness as fn

    return fn(pub_time)


@pytest.mark.unit
class TestNewsFreshness:
    def test_two_hours_old(self, frozen_market_clock):
        assert _freshness("2026-10-21 10:00") == pytest.approx(1 - 2 / 48)

    def test_forty_nine_hours_old_hits_floor(self, frozen_market_clock):
        assert _freshness("2026-10-19 11:00") == pytest.approx(0.1)

    def test_future_timestamp_is_capped(self, frozen_market_clock):
        assert _freshness("2026-10-22 09:00") == 1.0

    def test_unparsable_falls_back_to_neutral(self, frozen_market_clock):
        assert _freshness("昨天下午") == 0.5
        assert _freshness("") == 0.5

    def test_uses_market_clock_not_host_clock(self, monkeypatch):
        """把市场时钟往后拨 8 小时, 结果必须跟着变 —— 证明比的是北京时间。"""
        from ai_stock.quant import calendar_utils

        pub = "2026-10-21 10:00"
        monkeypatch.setattr(calendar_utils, "market_now", lambda: FROZEN)
        baseline = _freshness(pub)

        monkeypatch.setattr(
            calendar_utils, "market_now", lambda: FROZEN + timedelta(hours=8),
        )
        shifted = _freshness(pub)

        assert shifted < baseline
        assert shifted == pytest.approx(1 - 10 / 48)
