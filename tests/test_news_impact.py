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


def test_align_supplementary_theme_needs_two_points(monkeypatch):
    """LLM 给了一个主题、补第二个空位时，1 分的泛词命中不许补进来。

    线上事故：国金证券「AI 短剧出海」→ 传媒（AI应用）+ 风电（出海）。
    后者只靠"出海"这一个泛词（1 分），旧代码补位门槛是 1.0 就放进来了。
    """
    catalog = ["传媒（AI应用）", "风电（出海）"]
    monkeypatch.setattr(
        ni, "_theme_catalog",
        lambda: (catalog, {n: n for n in catalog}),
    )
    seen = {}

    def fake(text, limit=2, min_score=2.0):
        seen["min_score"] = min_score
        # 1 分的泛词命中：只有门槛低于 2 时才会被当作"第二个主题"
        return [{"name": "风电（出海）", "score": 1.0}] if min_score < 2.0 else []

    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes", fake, raising=False
    )
    out = ni.align_themes(
        "国金证券：当前AI短剧出海仍处渗透率低位，远期市场空间有望持续扩容",
        ["传媒（AI应用）"],
    )
    assert out == ["传媒（AI应用）"]
    assert seen["min_score"] == 2.0


def test_align_supplementary_theme_still_allowed_with_strong_hit(monkeypatch):
    """门槛提到 2 分不等于关掉补位：别名级别的强命中仍然可以补第二个主题。"""
    catalog = ["传媒（AI应用）", "风电（出海）"]
    monkeypatch.setattr(
        ni, "_theme_catalog",
        lambda: (catalog, {n: n for n in catalog}),
    )
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=2.0: [{"name": "风电（出海）", "score": 3.0}],
        raising=False,
    )
    out = ni.align_themes("短剧出海+海风招标", ["传媒（AI应用）"])
    assert out == ["传媒（AI应用）", "风电（出海）"]


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
        # strength 必须 ≥2 才算"可交易主题"（见 news_impact._tradable）
        item["industry_impacts"] = [
            {"name": "风电（出海）", "direction": "bullish", "strength": 3},
        ]
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


def test_rule_mode_skips_neutral_direction(monkeypatch):
    """规则判不出方向就不给主题：没方向的"传导链"等于没判断。

    「布伦特原油升至每桶 110 美元」只描述价格本身，rule_direction 给 neutral，
    这时配一个「油服」主题会让人误以为我们判断过方向。
    """
    monkeypatch.setattr(
        "ai_stock.quant.theme_registry.match_themes",
        lambda text, limit=2, min_score=1.0: [{"name": "风电（出海）", "score": 9.0}],
        raising=False,
    )
    news = [_n("布伦特原油期货价格自5月20日以来首次升至每桶110美元", "")]
    out = ni.enrich(ni.prepare(news), llm=None)
    assert out[0]["direction"] == "neutral"
    assert out[0]["industry_impacts"] == []


# ---------------------------------------------------------------------------
# 榜单二次评估（recheck）
# ---------------------------------------------------------------------------


def test_enrich_marks_assess_source(monkeypatch):
    """每条都要标出"这次结论是 LLM 给的还是规则推的"，二次评估靠它挑活。"""
    monkeypatch.setattr(ni, "assess", lambda items, llm=None: {})
    rule_only = ni.enrich(ni.prepare([_n("存储涨价：DRAM 报价上调 15%")]), llm=object())
    assert rule_only[0]["assess_source"] == "rule"

    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: {
            0: ni.NewsAssessment(
                index=0, event_type="industry", importance=6,
                direction="bullish", strength=2, themes=[],
            )
        },
    )
    via_llm = ni.enrich(ni.prepare([_n("某公司发布新品")]), llm=object(), use_cache=False)
    assert via_llm[0]["assess_source"] == "llm"


def test_recheck_only_assesses_rule_items(monkeypatch):
    """只补"上榜但没被 LLM 看过"的条目，一次批量调用，不重复花钱。"""
    calls: list[list[str]] = []

    def fake_assess(items, llm=None):
        calls.append([i["title"] for i in items])
        return {
            0: ni.NewsAssessment(
                index=0, event_type="industry", importance=9, direction="bullish",
                strength=3, confidence=0.6,
                themes=[ni.ThemeImpact(theme="风电（出海）", direction="bullish", strength=3)],
            )
        }

    monkeypatch.setattr(ni, "assess", fake_assess)
    monkeypatch.setattr(ni, "save_cached", lambda *a, **k: 0)

    done = ni.enrich(ni.prepare([_n("海上风电塔筒中标 12 亿元")]), llm=None)[0]
    done["assess_source"] = "llm"                     # 已经评估过，不该再送
    todo = ni.enrich(ni.prepare([_n("海上风电海缆招标落地")]), llm=None)[0]
    assert todo["assess_source"] == "rule"

    out = ni.recheck([done, todo], llm=object(), use_cache=False)

    assert calls == [["海上风电海缆招标落地"]]
    assert out[1]["assess_source"] == "llm"
    assert out[1]["industries"] == ["风电（出海）"]
    assert out[1]["confidence"] == 0.6                # 从规则票升级为 LLM 票
    assert out[0]["confidence"] < 0.5                # 已评估的没被动过


def test_recheck_is_fail_safe(monkeypatch):
    """LLM 挂了就保持规则结果，榜单不能因为二次评估挂掉。"""
    monkeypatch.setattr(ni, "assess", lambda items, llm=None: {})
    items = ni.enrich(ni.prepare([_n("海上风电塔筒中标 12 亿元")]), llm=None)
    before = [dict(i) for i in items]
    out = ni.recheck(items, llm=object(), use_cache=False)
    assert out[0]["industries"] == before[0]["industries"]
    assert out[0]["assess_source"] == "rule"


def _assess_one(
    theme="风电（出海）", direction="bullish", basis="海风订单↑→塔筒出货↑",
    strength=3, elasticity=1.0, reasoning="",
):
    return {
        0: ni.NewsAssessment(
            index=0, event_type="industry", importance=6, direction=direction,
            strength=strength, confidence=0.6, reasoning=reasoning,
            themes=[ni.ThemeImpact(
                theme=theme, direction=direction, strength=strength,
                elasticity=elasticity, basis=basis,
            )],
        )
    }


def test_neutral_theme_is_dropped(monkeypatch):
    """方向不明的主题不成立：给了 industry 却不说涨跌，观众会以为我们判断过。

    线上原样：北京「十五五」交通规划（轨道 1050 公里）被挂到
    「建筑（水利投资）」，basis 自己写着「水利方向无直接对应」。
    """
    monkeypatch.setattr(ni, "assess", lambda items, llm=None: _assess_one(direction="neutral"))
    out = ni.enrich(ni.prepare([_n("某市发布综合交通规划")]), llm=object(), use_cache=False)
    assert out[0]["industry_impacts"] == []
    assert out[0]["bull_bear_bias"] == "neutral"


def test_basis_that_negates_chain_drops_theme(monkeypatch):
    """basis 里自己写了"没有传导链"，主题就不该挂 —— 哪怕 direction 给了 bullish。"""
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            basis="长鑫科技参与战略轮融资仅为股权投资行为，不构成存储供需的传导链",
        ),
    )
    out = ni.enrich(ni.prepare([_n("某芯片公司完成战略融资")]), llm=object(), use_cache=False)
    assert out[0]["industries"] == []
    assert out[0]["expected_gain_high"] == 0.0

    # 反向：正常的传导链照旧保留
    monkeypatch.setattr(ni, "assess", lambda items, llm=None: _assess_one())
    out = ni.enrich(ni.prepare([_n("海上风电塔筒中标 12 亿元")]), llm=object(), use_cache=False)
    assert out[0]["industries"] == ["风电（出海）"]


def test_double_negative_basis_is_not_dropped():
    """「不构成利空」是给了方向，不是否定链条。"""
    assert not ni._basis_negates_chain("订单落地，不构成利空")
    assert ni._basis_negates_chain("无 A 股订单落地")
    assert ni._basis_negates_chain("链条偏弱，仅供观察")


# ---------------------------------------------------------------------------
# 传导级数：三级传导不能按一级受益给强度
# ---------------------------------------------------------------------------


def test_transmission_hops_counts_arrows():
    assert ni._transmission_hops("") == 0
    assert ni._transmission_hops("医保纳入→整机放量→零部件需求↑") == 2
    assert ni._transmission_hops("A→B->C=>D") == 3


def test_three_hop_chain_is_downgraded_not_dropped(monkeypatch):
    """线上原样（快照 975 第 1 位）：外骨骼医保 → 机器人（关节模组）+3.4~8.0%。

    3 跳链条，σ20 用人形机器人主题的 4%，算出来贴着单日上限。真问题是驱动因子
    不同源 —— 外骨骼的驱动是医保支付，不是「人形机器人量产」。降到 strength=1
    后幅度应落到 ±2% 以内。
    """
    monkeypatch.setattr(ni, "assess",
                        lambda items, llm=None: _assess_one(
                            theme="机器人（关节模组）", strength=3, elasticity=1.4,
                            basis="医保局规划→纳入北京上海江苏浙江多地医保→整机采购放量→关节模组需求↑",
                        ))
    # 正文里没有任何该主题的专指证据（这是"跨行业联想"的判据）
    monkeypatch.setattr(ni, "_theme_evidence", lambda text, names: set())
    out = ni.enrich(
        ni.prepare([_n("医用外骨骼康复训练项目纳入多地医保")]), llm=object(), use_cache=False,
    )
    assert out[0]["industries"] == ["机器人（关节模组）"]     # 保留主题
    assert out[0]["bias_strength"] == 1                      # 但只算微弱
    assert out[0]["expected_gain_high"] <= 2.0


def test_long_chain_with_evidence_is_not_downgraded(monkeypatch):
    """HBM 占比提升的报告 → 存储涨价，3 个箭头但正文里有专指证据（HBM）。

    只看箭头数会把这类降到微弱（线上回扫实测误伤 7 条，含光迅科技净利超预期），
    所以必须结合"正文里有没有该主题的专指证据"。
    """
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            theme="半导体（存储涨价）", strength=4, elasticity=1.0,
            basis="HBM 需求占比提升→存储颗粒涨价→模组厂成本上行→模组厂提价转嫁",
        ),
    )
    monkeypatch.setattr(
        ni, "_theme_evidence",
        lambda text, names: {"半导体（存储涨价）"},
    )
    out = ni.enrich(
        ni.prepare([_n("瑞银：预计 2027 年 HBM 占全球总需求比重将达到 39%")]),
        llm=object(), use_cache=False,
    )
    assert out[0]["bias_strength"] == 4
    assert out[0]["expected_gain_high"] > 2.0


def test_weak_and_long_chain_is_dropped(monkeypatch):
    """自己写"仅部分重叠、弹性存疑"，链条还要过 2 跳 → 不构成可交易映射。

    线上原样（快照 974 reasoning）：「与清单主题仅部分重叠，弹性存疑」。
    """
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            theme="机器人（关节模组）", strength=2, elasticity=1.3,
            basis="多地纳入医保→终端放量→外骨骼本体与减速器供应商订单增加",
            reasoning="与清单主题仅部分重叠，弹性存疑",
        ),
    )
    out = ni.enrich(ni.prepare([_n("医用外骨骼康复纳入多地医保")]), llm=object(), use_cache=False)
    assert out[0]["industries"] == []
    assert out[0]["expected_gain_high"] == 0.0


def test_normal_two_hop_chain_untouched(monkeypatch):
    """正常写法（HBM→涨价→毛利）不能被降权误伤。"""
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            theme="半导体（存储涨价）", strength=3, elasticity=1.2,
            basis="HBM 需求↑→存储涨价→模组厂毛利↑",
        ),
    )
    out = ni.enrich(ni.prepare([_n("DRAM 报价上调 15%")]), llm=object(), use_cache=False)
    assert out[0]["bias_strength"] == 3
    assert out[0]["expected_gain_high"] > 2.0


def test_weak_theme_does_not_get_rank_boost():
    """strength=1 的主题不该享受"命中主题"的置顶 —— 否则排序在奖励凑主题。"""
    weak = {"industry_impacts": [{"name": "机器人（关节模组）", "strength": 1}], "_order": 9.0}
    none = {"industry_impacts": [], "_order": 5.0}
    strong = {"industry_impacts": [{"name": "风电（出海）", "strength": 3}], "_order": 4.0}

    assert ni._tradable(weak) is False
    assert ni._tradable(none) is False
    assert ni._tradable(strong) is True
    # 微弱主题和空主题同组，靠 _order 决胜；强主题永远在前
    assert ni._rank_key(strong) > ni._rank_key(weak)
    assert ni._rank_key(weak)[0] == ni._rank_key(none)[0] == False   # noqa: E712


def test_weak_theme_does_not_get_rank_boost():
    """strength=1 的主题不该享受"命中主题"的置顶 —— 否则排序在奖励凑主题。"""
    weak = {"industry_impacts": [{"name": "机器人（关节模组）", "strength": 1}], "_order": 9.0}
    none = {"industry_impacts": [], "_order": 5.0}
    strong = {"industry_impacts": [{"name": "风电（出海）", "strength": 3}], "_order": 4.0}

    assert ni._tradable(weak) is False
    assert ni._tradable(none) is False
    assert ni._tradable(strong) is True
    # 微弱主题和空主题同组，靠 _order 决胜；强主题永远在前
    assert ni._rank_key(strong) > ni._rank_key(weak)
    assert ni._rank_key(weak)[0] == ni._rank_key(none)[0] == False   # noqa: E712


def test_theme_evidence_separates_keywords_from_stock_lookup(monkeypatch):
    """关键词那一路是本地查询、必须可靠；成分股名要联网，失败只少一路证据。

    首版把两路写在同一个 try 里，`match_themes` 又是函数内局部 import ——
    NameError 被 except 吞掉，`_theme_evidence` 恒定返回 None，整条降权逻辑
    静默失效（生产干跑"降权 0 个"就是这么来的）。
    """
    import ai_stock.quant.theme_registry as tr

    monkeypatch.setattr(tr, "match_themes", lambda text, limit=8, min_score=1.0: [{"name": "风电（出海）"}])
    monkeypatch.setattr(ni, "_themes_by_stock_name", lambda text, names: ["医药（创新药出海）"])
    assert ni._theme_evidence("海上风电塔筒中标", ["风电（出海）"]) == {
        "风电（出海）", "医药（创新药出海）",
    }

    # 成分股反查挂掉 → 仍返回关键词那一路的结果
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ni, "_themes_by_stock_name", boom)
    assert ni._theme_evidence("海上风电塔筒中标", ["风电（出海）"]) == {"风电（出海）"}

    # 关键词那一路挂掉 → 判定不了，返回 None（调用方不降权）
    monkeypatch.setattr(tr, "match_themes", boom)
    assert ni._theme_evidence("海上风电塔筒中标", ["风电（出海）"]) is None


def test_adjacent_sector_counts_as_in_universe(monkeypatch):
    """混合键合设备 → 先进封装：3 跳，但正文命中了「设备零部件」，属产业链内部延伸。

    判据是"这条新闻在不在主题体系覆盖的赛道内"，不是"这条新闻有没有命中**该**
    主题"—— 后者会把设备→封装、HBM→存储这类正确延伸一起降权。
    """
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            theme="半导体（存储涨价）", strength=3, elasticity=1.2,
            basis="HBM 占全球需求比重提升→高端 DRAM 产能挤压→存储颗粒涨价→模组厂毛利↑",
        ),
    )
    monkeypatch.setattr(
        ni, "_theme_evidence",
        lambda text, names: {"半导体（先进封装）"},      # 命中相邻主题即可
    )
    out = ni.enrich(
        ni.prepare([_n("瑞银：预计 2027 年 HBM 占全球总需求比重将达到 39%")]),
        llm=object(), use_cache=False,
    )
    assert out[0]["bias_strength"] == 3


# ---------------------------------------------------------------------------
# 「间接」不是自我否定：提示词要求写明间接，规则不能因此判它出局
# ---------------------------------------------------------------------------


def test_indirect_chain_is_capped_not_dropped(monkeypatch):
    """basis 写了"间接"、2 跳 → 保留主题并封顶 strength=2。

    首版把"间接"放进 ``_CHAIN_WEAK`` 一起丢弃，等于"谁守指令谁被判死刑"：
    提示词第 7 条明确要求间接传导在 basis 里写明「间接」。2026-10-19 快照 980
    整榜空行业，台积电 3nm、资本市场助力人形机器人两条都是这么没的。
    """
    keep, strength, elasticity = ni._temper_impact(
        5, 1.8,
        "台积电二厂获批转向3nm→海外晶圆厂资本开支结构调整→间接影响国产设备零部件验证节奏（间接）",
        "",
        in_universe=True,
    )
    assert keep is True
    assert strength == 2                  # 封到可交易线上限，不按一级驱动给满
    assert elasticity == 1.0


def test_self_denied_chain_is_still_dropped():
    """自述"弹性存疑/仅部分重叠"仍然是自我否定，2 跳以上照旧丢弃 ——
    放宽"间接"不能把这条一起放开。"""
    keep, _, _ = ni._temper_impact(
        3, 1.0, "医保规划→纳入多地医保→整机放量→关节模组需求↑（弹性存疑，仅部分重叠）",
        "", in_universe=True,
    )
    assert keep is False


# ---------------------------------------------------------------------------
# 多空与行业解耦：没有 L3 主题 ≠ 没有方向
# ---------------------------------------------------------------------------


def test_no_theme_still_keeps_market_level_direction(monkeypatch):
    """落不到 L3 但 LLM 给了方向 → 保留"整体偏多"，强度固定 1、scope=market。

    原来无主题一律 neutral，政策密集日整榜多空列全是"中性"，等于什么都没判断。
    但强度不能透传 LLM 的 5 —— 那会重演"指不出行业却显示强多"的老问题。
    """
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: {
            0: ni.NewsAssessment(
                index=0, event_type="policy", importance=6,
                direction="bullish", strength=5, confidence=0.6,
                themes=[],                      # 明确判定：无传导链
            )
        },
    )
    monkeypatch.setattr(ni, "_theme_evidence", lambda text, names: set())
    out = ni.enrich(
        ni.prepare([_n("证监会：支持更多外资机构来华投资展业")]),
        llm=object(), use_cache=False,
    )
    assert out[0]["industry_impacts"] == []
    assert out[0]["industries"] == []
    assert out[0]["bull_bear_bias"] == "bullish"
    assert out[0]["bias_strength"] == 1        # 封顶，不透传 5
    assert out[0]["bias_scope"] == "market"
    assert out[0]["expected_gain_low"] == 0.0  # 涨幅仍不给：没有标的就没有百分比


def test_theme_level_direction_is_scoped(monkeypatch):
    """有主题 → bias_scope=theme，强度按主题给（前端才敢显示"强多"）。"""
    monkeypatch.setattr(
        ni, "assess",
        lambda items, llm=None: _assess_one(
            theme="半导体（存储涨价）", strength=4, elasticity=1.0,
            basis="DRAM 现货涨价→模组厂采购价上行→毛利率提升",
        ),
    )
    monkeypatch.setattr(
        ni, "_theme_evidence", lambda text, names: {"半导体（存储涨价）"},
    )
    out = ni.enrich(
        ni.prepare([_n("DRAM 现货价环比涨 12%，原厂暂停报价")]),
        llm=object(), use_cache=False,
    )
    assert out[0]["bias_scope"] == "theme"
    assert out[0]["bias_strength"] == 4
    assert out[0]["expected_gain_high"] > 0


# ---------------------------------------------------------------------------
# 排序：新鲜度必须真的参与
# ---------------------------------------------------------------------------


def test_freshness_uses_time_field():
    """源数据字段是 ``time``，排序却读 ``pub_time`` —— 新鲜度曾恒为 0.5。

    后果是三天前的旧闻和刚出的消息同权，importance 相同就完全靠插入顺序。
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    fresh = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    stale = (now - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M")
    assert ni._pub_time({"time": fresh}) == fresh
    assert ni._pub_time({"pub_time": fresh}) == fresh
    assert ni._freshness(fresh) > ni._freshness(stale)


def test_rank_prefers_fresh_news_at_equal_importance():
    from datetime import datetime, timedelta

    now = datetime.now()
    fresh = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    stale = (now - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M")
    items = [
        {"title": "旧闻：三天前的行业消息", "content": "x", "time": stale,
         "source_count": 1, "importance": 4.0, "event_type": "industry"},
        {"title": "新消息：刚出的行业消息", "content": "x", "time": fresh,
         "source_count": 1, "importance": 4.0, "event_type": "industry"},
    ]
    out = ni.rank_with_quota([dict(i) for i in items], 2)
    assert out[0]["title"].startswith("新消息")


# ---------------------------------------------------------------------------
# 分批评估：40 条一次性送出会被 max_tokens 截断
# ---------------------------------------------------------------------------


def test_assess_splits_into_batches(monkeypatch):
    """下标必须映射回原列表，分批不能改变返回语义。

    40 条一次调用时输出（每条 200~300 字 reasoning）超限被截断，JSON 不完整，
    整批静默降级成规则 —— 2026-10-19 快照 981 的 20 条全是 confidence 0.35。
    """
    monkeypatch.setattr(ni, "ASSESS_BATCH", 12)
    calls: list[int] = []

    def fake_chunk(items, llm=None):
        calls.append(len(items))
        return {
            i: ni.NewsAssessment(index=i, event_type="industry", importance=5.0,
                                 direction="bullish", themes=[])
            for i in range(len(items))
        }

    monkeypatch.setattr(ni, "_assess_chunk", fake_chunk)
    items = [{"title": f"n{i}", "content": ""} for i in range(40)]
    out = ni.assess(items, llm=object())
    assert calls == [12, 12, 12, 4]
    assert sorted(out) == list(range(40))
    assert out[39].importance == 5.0


def test_assess_retries_failed_batch_in_halves(monkeypatch):
    """整批解析失败 → 对半重试，成功的半批仍然采纳。"""
    monkeypatch.setattr(ni, "ASSESS_BATCH", 12)
    calls: list[int] = []

    def fake_chunk(items, llm=None):
        calls.append(len(items))
        if len(items) == 12:          # 首批当作被截断
            return {}
        return {i: ni.NewsAssessment(index=i, event_type="industry",
                                     importance=6.0, direction="bullish")
                for i in range(len(items))}

    monkeypatch.setattr(ni, "_assess_chunk", fake_chunk)
    items = [{"title": f"n{i}", "content": ""} for i in range(12)]
    out = ni.assess(items, llm=object())
    assert calls == [12, 6, 6]
    assert len(out) == 12
    assert out[11].importance == 6.0

