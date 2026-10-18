"""新闻榜支撑能力：评估缓存 + 回测台账 + 历史回填口径.

为什么单独测缓存：缓存是"改了提示词却看不到效果"的头号原因 ——
key 里漏了版本号，旧结论会被一直复用。这里用测试把版本隔离锁死。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """临时库 + 重定向 db_ops._get_session（缓存/台账都走它）。"""
    from ai_stock.pipeline import db_models

    engine = create_engine(f"sqlite:///{tmp_path / 'support.db'}")
    db_models.ensure_support_tables(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(
        "ai_stock.pipeline.db_ops._get_session", lambda: factory()
    )
    return engine


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """:func:`enrich` 会查主题闭集，单测固定一份。"""
    from ai_stock.pipeline import news_impact as ni

    monkeypatch.setattr(
        ni, "_theme_catalog",
        lambda: (["风电（出海）"], {"风电（出海）": "风电（出海）"}),
    )
    monkeypatch.setattr(ni, "_themes_by_stock_name", lambda text, names: [])
    monkeypatch.setattr(ni, "_sigma_for", lambda theme: 0.015)


def _news(title: str, content: str = "内容") -> dict:
    return {"title": title, "content": content, "source": "测试源", "time": "2026-10-18 09:00"}


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def test_cache_roundtrip_preserves_fields(db):
    from ai_stock.pipeline import news_impact as ni

    item = _news("海上风电出海订单落地，塔筒中标 12 亿元")
    assessment = ni.NewsAssessment(
        index=0, event_type="industry", importance=8.2, direction="bullish",
        strength=4, horizon="1w", confidence=0.72, reasoning="中标金额明确",
        themes=[ni.ThemeImpact(theme="风电（出海）", direction="bullish", strength=4,
                               elasticity=1.3, basis="招标→塔筒订单")],
    )
    assert ni.save_cached([item], {0: assessment}) == 1

    hit = ni.load_cached([item])
    assert 0 in hit
    got = hit[0]
    assert got.event_type == "industry"
    assert got.importance == pytest.approx(8.2)
    assert got.direction == "bullish"
    assert got.themes[0].theme == "风电（出海）"
    assert got.index == 0          # 复用时下标必须归零，否则会串到别的新闻
    assert got.confidence == pytest.approx(0.72)


def test_cache_miss_when_prompt_version_changes(db, monkeypatch):
    from ai_stock.pipeline import news_impact as ni

    item = _news("海上风电出海订单落地")
    ni.save_cached([item], {0: ni.NewsAssessment(index=0, event_type="industry")})
    assert ni.load_cached([item])

    monkeypatch.setattr(ni, "PROMPT_VERSION", "999")
    assert ni.load_cached([item]) == {}


def test_cache_expires_after_ttl(db, monkeypatch):
    from ai_stock.pipeline import news_impact as ni

    item = _news("海上风电出海订单落地")
    ni.save_cached([item], {0: ni.NewsAssessment(index=0, event_type="industry")})
    # TTL 0 天 → 立刻过期（created_at 等于当前时刻，cutoff 也是当前时刻）
    assert ni.load_cached([item], ttl_days=-1) == {}


def test_cache_never_breaks_pipeline(db, monkeypatch):
    """缓存表被删了也不该让落库失败。"""
    from ai_stock.pipeline import news_impact as ni

    monkeypatch.setattr(
        "ai_stock.pipeline.db_ops._get_session",
        lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    assert ni.load_cached([_news("任意标题")]) == {}
    assert ni.save_cached([_news("任意标题")], {0: ni.NewsAssessment()}) == 0


def test_enrich_hits_cache_on_second_pass(db, monkeypatch):
    """同一批新闻第二次评估必须全部命中缓存，不再调 LLM。"""
    from ai_stock.pipeline import news_impact as ni

    calls: list[int] = []

    def fake_assess(items, llm=None):
        calls.append(len(items))
        return {
            i: ni.NewsAssessment(index=i, event_type="industry", importance=7.0,
                                 direction="bullish", strength=3, confidence=0.6)
            for i in range(len(items))
        }

    monkeypatch.setattr(ni, "assess", fake_assess)
    prepared = ni.prepare([
        _news("海上风电出海订单落地，塔筒中标"),
        _news("储能招标规模超预期，系统集成商受益"),
        _news("核电项目核准，主设备招标启动"),
    ])
    assert len(prepared) == 3

    ni.enrich(prepared, llm=object())
    assert calls == [3]
    ni.enrich(prepared, llm=object())
    assert calls == [3], "第二次应全部命中缓存"

    # 关掉缓存则照常调用
    ni.enrich(prepared, llm=object(), use_cache=False)
    assert calls == [3, 3]


# ---------------------------------------------------------------------------
# 回测台账
# ---------------------------------------------------------------------------


def test_record_tracking_writes_one_row_per_theme(db):
    from ai_stock.pipeline import news_impact as ni

    items = [{
        "title": "海上风电出海订单落地，塔筒中标 12 亿元",
        "horizon": "1d",
        "industry_impacts": [
            {"name": "风电（出海）", "direction": "bullish", "strength": 4,
             "exp_low": 1.2, "exp_high": 2.8},
            {"name": "储能（PCS/系统集成）", "direction": "bullish", "strength": 2,
             "exp_low": 0.4, "exp_high": 1.0},
        ],
    }]
    assert ni.record_tracking(items, snapshot_id=42, base_date="2026-10-16") == 2

    with db.connect() as conn:
        rows = conn.execute(text(
            "SELECT theme, direction, base_date, exp_high, actual_return "
            "FROM news_impact_tracking WHERE snapshot_id = 42 ORDER BY theme"
        )).fetchall()
    assert len(rows) == 2
    assert rows[0][2] == "2026-10-16"
    assert rows[0][4] is None       # 待验证
    assert {r[0] for r in rows} == {"风电（出海）", "储能（PCS/系统集成）"}


def test_record_tracking_skips_duplicate_and_empty(db):
    from ai_stock.pipeline import news_impact as ni

    dup = {
        "title": "同一条新闻",
        "horizon": "1d",
        "industry_impacts": [{"name": "风电（出海）", "direction": "bullish"}],
    }
    # 同一标题+主题出现两次只写一行
    assert ni.record_tracking([dup, dict(dup)], snapshot_id=7) == 1
    # 无主题的新闻不产生台账
    assert ni.record_tracking(
        [{"title": "金价跌破", "industry_impacts": []}], snapshot_id=7
    ) == 0
