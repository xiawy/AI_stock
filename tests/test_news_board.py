"""Tests for the news board (新闻榜) — 采集的新闻 → 富化 → 配额排序 → 落库.

Covers:
- select_top_news: 配额排序（政策 ≤8、多类型保底）、截断、无标题剔除、不改入参
- save_news_board: 空输入短路、快照节流、force 绕过、db_ops 调用链、异常吞掉
- _to_news_row: 事件维度字段映射
- normalize_title / dedup_news (同一新闻多源转发去重)
"""

from __future__ import annotations

import pytest

from ai_stock.pipeline import news_impact
from ai_stock.pipeline.news_board import (
    _to_news_row,
    dedup_news,
    normalize_title,
    save_news_board,
    select_top_news,
)


@pytest.fixture(autouse=True)
def _no_theme_db(monkeypatch):
    """select/enrich 会查主题库；单测用固定闭集，不碰真库。"""
    monkeypatch.setattr(
        news_impact, "_theme_catalog",
        lambda: (["半导体（存储涨价）", "风电（出海）"], {
            "存储涨价": "半导体（存储涨价）",
            "半导体（存储涨价）": "半导体（存储涨价）",
            "风电（出海）": "风电（出海）",
        }),
    )
    monkeypatch.setattr(news_impact, "_themes_by_stock_name", lambda text, names: [])
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [], raising=False,
    )
    monkeypatch.setattr(news_impact, "_stock_cache", {})
    monkeypatch.setattr(news_impact, "_sigma_for", lambda theme: 0.015)
    # 落库前会做一次幂等补列；单测不碰真库
    monkeypatch.setattr(
        "ai_stock.pipeline.db_models.ensure_news_impact_schema",
        lambda *a, **k: None,
    )


def _item(title, time, category="news"):
    return {
        "title": title,
        "content": f"{title} content",
        "source": "东方财富",
        "time": time,
        "category": category,
        "title_hash": title[:6],
    }


# ---------------------------------------------------------------------------
# select_top_news
# ---------------------------------------------------------------------------


def test_select_guarantees_type_diversity():
    """30 条政策 + 30 条其它类型 → 政策不得垄断（原实现必然 100% 政策）。"""
    news = [
        _item(f"政策{i}：国务院部署专项工作", f"2026-08-30 09:{i:02d}", "policy")
        for i in range(30)
    ]
    others = [
        ("半导体存储涨价，DRAM 报价上调", "industry"),
        ("海上风电出海订单落地，塔筒中标", "industry"),
        ("三代制冷剂配额落地，R32 报价上调", "policy"),  # 含"配额"判成政策
        ("组件价格下调，光伏行业承压", "industry"),
        ("工业硅开工率下滑，库存累积", "industry"),
        ("硅料报价企稳，排产回升", "industry"),
        ("某公司公告：中标 3 亿元订单", "company"),
        ("某公司：股东减持 2% 股份", "company"),
        ("某公司：拟收购同行资产", "company"),
        ("某公司：发布澄清公告", "company"),
        ("某公司：前三季度净利润增长", "company"),
        ("某公司：签订战略合作协议", "company"),
        ("北向资金净流入 50 亿元", "capital"),
        ("两融余额回升至 1.8 万亿", "capital"),
        ("主力资金加仓电子板块", "capital"),
        ("ETF 净申购 30 亿元", "capital"),
        ("美联储降息预期升温", "overseas"),
        ("欧盟对中国电动车加征关税", "overseas"),
        ("英伟达上调业绩指引", "overseas"),
        ("台积电上调资本开支", "overseas"),
    ]
    for i, (title, _t) in enumerate(others):
        news.append(_item(title, f"2026-08-30 08:{i:02d}"))
    top = select_top_news(news, top_n=20)
    assert len(top) == 20
    policy_count = sum(1 for n in top if n["event_type"] == "policy")
    assert policy_count <= news_impact.QUOTA_MAX["policy"]
    assert len({n["event_type"] for n in top}) >= 4


def test_select_truncates_and_ranks_contiguous():
    news = [_item(f"n{i}", f"2026-08-30 08:{i:02d}") for i in range(30)]
    top = select_top_news(news, top_n=20)
    assert len(top) == 20
    assert [n["rank"] for n in top] == list(range(1, 21))


def test_select_drops_empty_titles_and_keeps_input():
    news = [_item("ok", "2026-08-30 08:00"), {"title": "", "time": "x"}]
    top = select_top_news(news, top_n=20)
    assert len(top) == 1
    # 入参不被修改 (rank 只加在拷贝上)
    assert "rank" not in news[0]


def test_select_empty_input():
    assert select_top_news([], top_n=20) == []
    assert select_top_news(None, top_n=20) == []


def test_select_dedups_then_ranks_contiguous():
    news = [
        _item("动力电池竞争秩序，加强监测", "2026-08-30 09:00"),
        _item("动力电池竞争秩序 加强监测", "2026-08-30 08:00"),
        _item("另一条独立新闻", "2026-08-30 07:00"),
    ]
    top = select_top_news(news, top_n=20)
    assert len(top) == 2
    assert [n["rank"] for n in top] == [1, 2]


# ---------------------------------------------------------------------------
# save_news_board
# ---------------------------------------------------------------------------


def test_save_empty_returns_none():
    assert save_news_board([])["snapshot_id"] is None


def test_save_writes_snapshot_and_items(monkeypatch):
    calls: dict = {}

    from ai_stock.pipeline import db_ops

    monkeypatch.setattr(db_ops, "get_recent_snapshot_titles", lambda hours=2.0: None)
    monkeypatch.setattr(
        db_ops, "create_snapshot",
        lambda period, total_news, status: calls.update(
            period=period, total=total_news, status=status,
        ) or 42,
    )
    monkeypatch.setattr(
        db_ops, "save_news_items",
        lambda snapshot_id, rows: calls.update(snapshot_id=snapshot_id, rows=rows)
        or len(rows),
    )
    monkeypatch.setattr(
        db_ops, "update_snapshot",
        lambda snapshot_id, **kwargs: calls.update(done_status=kwargs.get("status")) or True,
    )

    news = [_item("工信部：规范行业竞争秩序", "2026-08-30 08:00", "policy")] + [
        _item(f"news-{i}", f"2026-08-29 08:{i:02d}") for i in range(25)
    ]
    result = save_news_board(news, top_n=20)

    assert result["snapshot_id"] == 42
    assert result["saved"] == 20
    assert calls["total"] == 26
    assert calls["snapshot_id"] == 42
    assert calls["done_status"] == "completed"
    assert len(calls["rows"]) == 20


def test_save_throttles_when_content_unchanged(monkeypatch):
    from ai_stock.pipeline import db_ops

    news = [_item(f"n{i}", f"2026-08-30 08:{i:02d}") for i in range(10)]
    titles = [n["title"] for n in news]

    monkeypatch.setattr(
        db_ops, "get_recent_snapshot_titles",
        lambda hours=2.0: {"snapshot_id": 7, "titles": titles},
    )
    monkeypatch.setattr(
        db_ops, "update_snapshot",
        lambda snapshot_id, **kwargs: snapshot_id == 7,
    )
    monkeypatch.setattr(
        db_ops, "create_snapshot",
        lambda **kwargs: pytest.fail("节流命中时不应新建快照"),
    )

    result = save_news_board(news, top_n=20)
    assert result == {"snapshot_id": 7, "saved": 0, "skipped": "throttled"}


def test_save_force_bypasses_throttle(monkeypatch):
    from ai_stock.pipeline import db_ops

    news = [_item(f"n{i}", f"2026-08-30 08:{i:02d}") for i in range(10)]
    monkeypatch.setattr(
        db_ops, "get_recent_snapshot_titles",
        lambda hours=2.0: {"snapshot_id": 7, "titles": [n["title"] for n in news]},
    )
    monkeypatch.setattr(db_ops, "create_snapshot", lambda **kwargs: 99)
    monkeypatch.setattr(db_ops, "save_news_items", lambda sid, rows: len(rows))
    monkeypatch.setattr(db_ops, "update_snapshot", lambda sid, **kwargs: True)

    result = save_news_board(news, top_n=20, force=True)
    assert result["snapshot_id"] == 99
    assert result["saved"] == len(news)


def test_save_swallows_db_errors(monkeypatch):
    from ai_stock.pipeline import db_ops

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_ops, "get_recent_snapshot_titles", lambda hours=2.0: None)
    monkeypatch.setattr(db_ops, "create_snapshot", boom)
    result = save_news_board([_item("x", "2026-08-30 08:00")])
    assert result["snapshot_id"] is None


# ---------------------------------------------------------------------------
# _to_news_row
# ---------------------------------------------------------------------------


def test_to_news_row_maps_event_fields():
    item = _item("存储涨价 DRAM 报价上调", "2026-08-30 08:00", "policy") | {
        "rank": 3,
        "event_type": "industry",
        "importance": 7.5,
        "bias_strength": 3,
        "horizon": "3d",
        "confidence": 0.8,
        "source_count": 4,
        "cluster_id": "abc123",
        "industries": ["半导体（存储涨价）"],
        "industry_impacts": [{
            "name": "半导体（存储涨价）", "direction": "bullish",
            "strength": 3, "elasticity": 1.2,
            "exp_low": 1.35, "exp_high": 3.15, "basis": "涨价函",
        }],
        "bull_bear_bias": "bullish",
        "expected_gain_low": 1.35,
        "expected_gain_high": 3.15,
        "reasoning": "原厂发出涨价函",
    }
    row = _to_news_row(item)

    assert row["pub_time"] == "2026-08-30 08:00"
    assert row["event_type"] == "industry"
    assert row["importance"] == 7.5
    assert row["bias_strength"] == 3
    assert row["horizon"] == "3d"
    assert row["confidence"] == 0.8
    assert row["source_count"] == 4
    assert row["cluster_id"] == "abc123"
    assert row["rank"] == 3
    assert row["composite_score"] == 7.5
    assert "半导体（存储涨价）" in row["industry_impacts_json"]
    assert row["expected_gain_high"] == 3.15
    # 四维分只在本类维度上等于 importance，其余为 0（不做假评分）
    assert row["news_score"] == 7.5
    assert row["policy_score"] == 0.0


def test_to_news_row_defaults_are_safe():
    row = _to_news_row(_item("t", "2026-08-30 08:00"))
    assert row["event_type"] == ""
    assert row["industry_impacts_json"] == "[]"
    assert row["source_count"] == 1
    assert row["bull_bear_bias"] == "neutral"


# ---------------------------------------------------------------------------
# normalize_title / dedup_news (同一新闻多源转发去重)
# ---------------------------------------------------------------------------


def test_normalize_title_ignores_punct_space_and_brackets():
    assert normalize_title(
        "工信部：规范动力电池竞争秩序，加强监测"
    ) == normalize_title("工信部：规范动力电池竞争秩序 加强监测")
    assert normalize_title(
        "【零售试点城市名单公布】记者3日从商务部获悉"
    ) == normalize_title("零售试点城市名单公布")


def test_dedup_removes_multisource_duplicates_keeps_first():
    news = [
        _item("动力电池竞争秩序，加强监测", "2026-08-30 09:00"),
        _item("动力电池竞争秩序 加强监测", "2026-08-30 08:00"),
    ]
    out = dedup_news(news)
    assert len(out) == 1
    assert out[0]["time"] == "2026-08-30 09:00"


def test_dedup_keeps_rewritten_titles():
    news = [
        _item("华康洁净：中标项目遭废标", "2026-08-30 09:00"),
        _item("华康洁净：9018.65万元中标项目被废标", "2026-08-30 08:00"),
    ]
    assert len(dedup_news(news)) == 2
