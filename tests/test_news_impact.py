"""Tests for the news impact pipeline (新闻影响力流水线).

覆盖设计文档 ``docs/NEWS_IMPACT_REDESIGN.md`` 的四条硬约束：

1. 类型多样性 —— 配额保证政策不垄断
2. 事件聚类 —— 同事件多源合并，但榜单不缩水
3. 主题对齐 —— 只认 active 的 L3 主题，落不到就留空（不回落到大行业）
4. 幅度校准 —— 由 σ 锚定并截断，LLM 不碰百分比
5. 降级 —— LLM 不可用时字段仍有值
"""

from __future__ import annotations

import pytest

from ai_stock.pipeline import news_impact as ni

THEMES = ["半导体（存储涨价）", "风电（出海）", "机器人（关节模组）"]
ALIASES = {
    "存储涨价": "半导体（存储涨价）",
    "半导体（存储涨价）": "半导体（存储涨价）",
    "风电（出海）": "风电（出海）",
    "机器人（关节模组）": "机器人（关节模组）",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """固定主题闭集，不查库、不请求行情。"""
    monkeypatch.setattr(ni, "_theme_catalog", lambda: (THEMES, ALIASES))
    monkeypatch.setattr(ni, "_themes_by_stock_name", lambda text, names: [])
    monkeypatch.setattr(ni, "_sigma_for", lambda theme: 0.015)
    monkeypatch.setattr(ni, "_stock_cache", {})
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [], raising=False,
    )


def _n(title, content="", source="东方财富", time="2026-10-18 08:00"):
    return {"title": title, "content": content, "source": source, "time": time}


# ---------------------------------------------------------------------------
# 规则分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("工信部印发指导意见，规范行业竞争秩序", "policy"),
    ("美联储降息预期升温，全球风险资产上涨", "overseas"),
    ("北向资金净流入 82 亿元，两融余额回升", "capital"),
    ("某公司公告：中标 3 亿元订单", "company"),
    ("三代制冷剂配额落地，R32 报价上调", "policy"),   # 配额属政策工具，平局时政策优先
])
def test_classify_event(text, expected):
    assert ni.classify_event(text) == expected


def test_classify_company_beats_capital_on_tie():
    """「股东减持」同时命中公司行为与资金面，应归公司。"""
    assert ni.classify_event("某公司：股东减持 2% 股份，发布公告") == "company"


def test_rule_direction_and_strength():
    assert ni.rule_direction("中标 3 亿，订单翻倍，涨价超预期")[0] == "bullish"
    assert ni.rule_direction("停产 亏损 下滑 处罚")[0] == "bearish"
    assert ni.rule_direction("公司召开股东大会")[0] == "neutral"
    assert ni.rule_direction("涨价 中标 突破 放量 供不应求")[1] == 3


# ---------------------------------------------------------------------------
# 聚类
# ---------------------------------------------------------------------------


def test_cluster_marks_multisource_without_dropping_items():
    news = [
        _n("动力电池竞争秩序，加强监测", "工信部部署", "东方财富"),
        _n("动力电池竞争秩序 加强监测", "工信部部署相关工作", "同花顺"),
        _n("光伏组件价格下跌", "组件报价下调", "新浪财经"),
    ]
    out = ni.cluster_news(news)
    assert len(out) == 3                     # 标记而非删除
    same = [i for i in out if "动力电池" in i["title"]]
    assert len({i["cluster_id"] for i in same}) == 1
    assert same[0]["source_count"] == 2
    other = [i for i in out if "光伏" in i["title"]][0]
    assert other["cluster_id"] != same[0]["cluster_id"]


def test_cluster_does_not_merge_rewritten_titles():
    news = [
        _n("华康洁净：中标项目遭废标"),
        _n("华康洁净：9018.65万元中标项目被废标"),
    ]
    out = ni.cluster_news(news)
    assert len({i["cluster_id"] for i in out}) == 2


# ---------------------------------------------------------------------------
# 主题对齐（闭集纪律）
# ---------------------------------------------------------------------------


def test_align_accepts_only_catalog_names():
    assert ni.align_themes("随便什么", ["半导体（存储涨价）"]) == ["半导体（存储涨价）"]


def test_align_normalizes_alias():
    assert ni.align_themes("随便什么", ["存储涨价"]) == ["半导体（存储涨价）"]


def test_align_rejects_invented_and_coarse_industry():
    """LLM 自创的「半导体」是粗行业，不在闭集内 → 丢弃，不回落。"""
    assert ni.align_themes("半导体行业景气上行", ["半导体", "新能源", "科技"]) == []


def test_align_limits_to_two():
    out = ni.align_themes("x", THEMES, limit=2)
    assert len(out) <= 2


def test_align_uses_keyword_match_when_llm_silent(monkeypatch):
    """LLM 没给主题名、但调用方允许兜底时（如纯规则模式），关键词仍可命中。"""
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "风电（出海）", "score": 3.0}],
        raising=False,
    )
    assert ni.align_themes("海上风电塔筒出海订单", []) == ["风电（出海）"]


def test_align_can_be_barred_from_keyword_match(monkeypatch):
    """``allow_match=False`` 时只认闭集：LLM 说没有传导链就不许关键词补。"""
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "风电（出海）", "score": 3.0}],
        raising=False,
    )
    assert ni.align_themes("海上风电塔筒出海订单", [], allow_match=False) == []


# ---------------------------------------------------------------------------
# 幅度校准
# ---------------------------------------------------------------------------


def test_calibrate_scales_with_strength():
    weak = ni.calibrate("bullish", 1, 1.0, 0.015)
    strong = ni.calibrate("bullish", 5, 1.0, 0.015)
    assert 0 < weak[1] < strong[1]
    # coef[5]/coef[1] = 2.2/0.3 ≈ 7.3 倍
    assert strong[1] / weak[1] == pytest.approx(2.2 / 0.3, rel=0.05)


def test_calibrate_is_symmetric_for_bearish():
    up = ni.calibrate("bullish", 3, 1.0, 0.015)
    dn = ni.calibrate("bearish", 3, 1.0, 0.015)
    assert dn[0] < 0 and dn[1] <= 0
    assert dn[0] == pytest.approx(-up[1], rel=0.01)
    assert dn[1] == pytest.approx(-up[0], rel=0.01)


def test_calibrate_neutral_is_zero():
    assert ni.calibrate("neutral", 3, 1.0, 0.015) == (0.0, 0.0)


def test_calibrate_clamps_extremes():
    """理论值可达 ±44%，但展示上界不许超过 ±8%（单日）/ ±15%（多日）。"""
    _, high = ni.calibrate("bullish", 5, 2.0, 0.10)
    assert high <= ni.CAP_SINGLE_DAY * 100 + 0.01
    _, high3 = ni.calibrate("bullish", 5, 2.0, 0.10, horizon="1w")
    assert high3 <= ni.CAP_MULTI_DAY * 100 + 0.01


# ---------------------------------------------------------------------------
# 配额排序
# ---------------------------------------------------------------------------


def _typed(event_type, n, prefix="t", importance=5.0):
    return [
        {
            "title": f"{prefix}{i}", "event_type": event_type,
            "importance": importance, "pub_time": "2026-10-18 08:00",
            "source_count": 1, "cluster_id": f"{prefix}{i}",
        }
        for i in range(n)
    ]


def test_quota_caps_policy_and_keeps_other_types():
    items = _typed("policy", 30, "p") + _typed("industry", 8, "i") + \
        _typed("company", 5, "c") + _typed("capital", 4, "m") + _typed("overseas", 3, "o")
    top = ni.rank_with_quota(items, 20)
    counts = {}
    for t in top:
        counts[t["event_type"]] = counts.get(t["event_type"], 0) + 1
    assert len(top) == 20
    assert counts["policy"] <= ni.QUOTA_MAX["policy"]
    for etype, minimum in ni.QUOTA_MIN.items():
        assert counts.get(etype, 0) >= minimum


def test_quota_never_fills_with_low_importance():
    items = _typed("policy", 5, "p", importance=9.0) + \
        _typed("company", 5, "c", importance=1.0)   # 例行公告
    top = ni.rank_with_quota(items, 20)
    assert all(t["importance"] >= ni.QUOTA_MIN_IMPORTANCE for t in top)
    assert len(top) == 5


def test_quota_keeps_one_item_per_cluster():
    """同事件多源转发只占一个榜位（候选人充足时）。"""
    items = _typed("industry", 4, "a")
    for item in items:
        item["cluster_id"] = "same"
    items += _typed("industry", 10, "b")          # 10 条独立事件
    top = ni.rank_with_quota(items, 6)
    assert len(top) == 6
    assert len([t for t in top if t["cluster_id"] == "same"]) == 1


def test_quota_does_not_duplicate_same_event_when_pool_is_scarce():
    """候选池只有同一事件的 5 条转发 → 只占 1 席，用重复凑满没有意义。"""
    items = _typed("industry", 5, "a")
    for item in items:
        item["cluster_id"] = "same"
    top = ni.rank_with_quota(items, 5)
    assert len(top) == 1


# ---------------------------------------------------------------------------
# 端到端（规则降级路径）
# ---------------------------------------------------------------------------


def test_prepare_and_enrich_rule_mode_fills_fields():
    news = [
        _n("存储涨价：DRAM 报价上调 15%", "原厂发出涨价函，模组厂受益。", "东方财富"),
        _n("海上风电出海订单落地，塔筒中标 8 亿元", "海外订单放量。", "同花顺"),
        _n("某公司召开股东大会", "审议相关议案。", "东方财富"),
    ]
    prepared = ni.prepare(news)
    assert len(prepared) == 3
    assert all(p["event_type"] for p in prepared)
    assert all(p["importance"] >= 1 for p in prepared)

    enriched = ni.enrich(prepared, llm=None)
    top = ni.rank_with_quota(enriched, 3)
    # 「股东大会议案」属例行公告，规则分低于门槛 → 不上榜（宁缺毋滥）
    assert len(top) == 2
    assert all("股东大会" not in item["title"] for item in top)
    for item in top:
        assert item["event_type"] in ni.EVENT_TYPES
        assert item["cluster_id"]
        assert item["importance"] > 0
        assert isinstance(item["industry_impacts"], list)
        # 行业落不到主题时必须是空列表，不能回落到「半导体」这类大行业
        assert all(i["name"] in THEMES for i in item["industry_impacts"])


def test_enrich_records_zero_gain_when_no_theme():
    news = [_n("某地发布城市规划", "与产业无关。")]
    enriched = ni.enrich(ni.prepare(news), llm=None)
    assert enriched[0]["expected_gain_low"] == 0.0
    assert enriched[0]["expected_gain_high"] == 0.0


def test_content_diff_uses_containment_not_jaccard():
    """上期榜单（20 条）完整包含在候选池（40 条）里 → 差异为 0，节流生效。"""
    pool = {f"t{i}" for i in range(40)}
    last = {f"t{i}" for i in range(20)}
    assert ni.content_diff(pool, last) == 0.0
    assert ni.content_diff(set(), last) == 1.0
    half = {f"t{i}" for i in range(10)} | {f"x{i}" for i in range(10)}
    assert ni.content_diff(pool, half) == 0.5


def test_theme_linked_news_outranks_macro_noise():
    """能落到主题的条目排前面：原油/金价这类背景信息不该压住可交易信号。"""
    macro = _typed("overseas", 2, "m", importance=8.0)
    for item in macro:
        item["industry_impacts"] = []
        item["source_count"] = 3
    signal = _typed("company", 2, "s", importance=6.0)
    for item in signal:
        item["industry_impacts"] = [{"name": "风电（出海）", "direction": "bullish"}]
    top = ni.rank_with_quota(macro + signal, 4)
    assert len(top) == 4                      # 配额保底: 海外仍在榜
    assert top[0]["industry_impacts"]         # 但榜首是能落到主题的
    assert top[0]["title"].startswith("s")
    assert top[-1]["industry_impacts"] == []  # 背景信息沉到末尾


def test_no_theme_means_neutral_direction():
    """落不到主题时不显示多空：没有承受对象的方向箭头是噪音。"""
    news = [_n("暗盘金价跌破 4150 美元/盎司", "外盘商品波动。")]
    enriched = ni.enrich(ni.prepare(news), llm=None)
    assert enriched[0]["industry_impacts"] == []
    assert enriched[0]["bull_bear_bias"] == "neutral"
    assert enriched[0]["bias_strength"] == 0


# ---------------------------------------------------------------------------
# LLM 空主题 vs 规则兜底的边界
# ---------------------------------------------------------------------------


def test_llm_empty_themes_blocks_rule_fallback(monkeypatch):
    """LLM 判定"没有传导链"时，关键词匹配不得把它救回来。

    线上实测过的两个错例：「AI 短剧出海」被关键词"出海"认成风电、
    「纽约原油暗盘突破」被认成油服并顶到榜首 —— 规则命中的词恰恰是
    LLM 刚刚否掉的那条新闻。
    """
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: {
            0: ni.NewsAssessment(
                index=0, event_type="industry", importance=6,
                direction="neutral", themes=[],      # 明确判定：无传导链
            )
        },
    )
    # 关键词匹配"本来能命中"，用来验证它被 allow_match=False 拦住
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "风电（出海）", "score": 9.0}],
        raising=False,
    )

    news = [_n("AI 短剧出海仍处渗透率低位", "券商研报：远期市场空间有望扩张")]
    out = ni.enrich(ni.prepare(news), llm=object(), use_cache=False)
    assert out[0]["industry_impacts"] == []
    assert out[0]["industries"] == []
    assert out[0]["bull_bear_bias"] == "neutral"
    assert out[0]["bias_strength"] == 0


def test_llm_theme_name_is_normalized_by_keywords(monkeypatch):
    """反过来的情况：LLM 给了主题名（哪怕是旧名/别名），仍要归一到闭集。"""
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: {
            0: ni.NewsAssessment(
                index=0, event_type="industry", importance=7, direction="bullish",
                strength=3,
                themes=[ni.ThemeImpact(theme="海上风电", direction="bullish", strength=3)],
            )
        },
    )
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "风电（出海）", "score": 8.0}],
        raising=False,
    )

    news = [_n("海上风电塔筒中标 12 亿元", "欧洲订单落地")]
    out = ni.enrich(ni.prepare(news), llm=object(), use_cache=False)
    assert out[0]["industries"] == ["风电（出海）"]


def test_rule_mode_skips_ceremonial_news(monkeypatch):
    """规则模式必须和 LLM 守同一口径：仪式性新闻不给主题。

    实测过「中国航展海上装备展示区将带来全新体验」被关键词匹配到
    「机器人（关节模组）」—— 有活动、无结果，谈不上传导链。
    """
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "机器人（关节模组）", "score": 9.0}],
        raising=False,
    )
    news = [_n("中国航展海上装备展示区将带来全新体验", "观众可现场参观")]
    out = ni.enrich(ni.prepare(news), llm=None)
    assert out[0]["industry_impacts"] == []

    # 有量化结果的同类新闻不受影响（"3 亿元"是结果，不是活动）
    news2 = [_n("航展现场签约 3 亿元订单，关节模组批量供货", "")]
    out2 = ni.enrich(ni.prepare(news2), llm=None)
    assert out2[0]["industries"] == ["机器人（关节模组）"]


def test_is_ceremonial_requires_no_quantified_result():
    assert ni.is_ceremonial("第三届全球数字贸易博览会开幕")
    assert ni.is_ceremonial("公司举行揭牌仪式")
    assert not ni.is_ceremonial("论坛现场签约 12 亿元项目")
    assert not ni.is_ceremonial("公司扩产 5 万吨产能")
