"""新闻影响力流水线：把"快讯列表"变成"可交易的判断链"。

设计见 ``docs/NEWS_IMPACT_REDESIGN.md``。八步流水线：

```
1 gather      get_hot_news(3d)                     ~670 条（macro_event 已抓）
2 normalize   title 规范化去重                      复用 news_board.dedup_news
3 prefilter   规则初筛 670 → 120                   本模块
4 cluster     同事件多源合并 → source_count         本模块
5 assess      批量 LLM 结构化评估 120 → 40          本模块（核心）
6 align       行业落到 L3 主题（闭集，禁自创）       本模块 + theme_registry
7 calibrate   幅度由主题 σ 统计锚定，不让 LLM 猜     本模块
8 rank        配额排序 → Top20                      本模块
```

三条硬纪律（对应线上问题）：

- **配额保证多样性**：政策 ≤ 8、行业 ≥ 6、公司 ≥ 3、资金 ≥ 2、海外 ≥ 1。
  原实现按「是否政策」双键倒序，677 条里政策 ≥20 就必然 100% 政策 —— 配额
  是结构性保证，不是调权重碰运气。
- **幅度不许 LLM 猜**：LLM 只给 direction/strength(1~5)/elasticity，百分比由
  ``幅度 = coef[strength] × 主题 σ20 × elasticity`` 算出并截断。没有波动率概念的
  模型会吐"预期涨 20%"这种不可交易的数字。
- **行业不许自创**：候选闭集 = 当前 ``active`` 的 L3 主题。闭集里没有就留空，
  **不回落到「半导体」这类大行业** —— 那正是用户要消灭的东西。

LLM 不可用/超时/解析失败时整体降级为规则模式（关键词分类 + 规则重要性 +
主题关键词匹配），榜单仍然有类型/行业/多空/幅度，不会退回全 0。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from statistics import median
from typing import Iterable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── 配额（改这里等于改榜单结构，务必同步文档 §4.4）────────────────────────
EVENT_TYPES: tuple[str, ...] = ("policy", "industry", "company", "capital", "overseas")
EVENT_TYPE_LABELS = {
    "policy": "政策",
    "industry": "行业",
    "company": "公司",
    "capital": "资金",
    "overseas": "海外",
}
# 上限项（policy）是硬约束，下限项是保底；某类不足时名额让给其它类。
QUOTA_MAX: dict[str, int] = {"policy": 8}
QUOTA_MIN: dict[str, int] = {"industry": 6, "company": 3, "capital": 2, "overseas": 1}
# 保底名额的门槛：低于此分的是例行公告/流水快讯，宁可不占位
QUOTA_MIN_IMPORTANCE = 2.0

# ── 幅度校准 ────────────────────────────────────────────────────────────
STRENGTH_COEF = {1: 0.3, 2: 0.6, 3: 1.0, 4: 1.5, 5: 2.2}
HORIZON_FACTOR = {"1d": 1.0, "3d": 1.5, "1w": 2.2}
CAP_SINGLE_DAY = 0.08          # 单日预期截断 ±8%
CAP_MULTI_DAY = 0.15           # 多日累计截断 ±15%
DEFAULT_SIGMA = 0.012          # 主题 σ 与市场 σ 都拿不到时的兜底日波动

# ── 初筛规模 ────────────────────────────────────────────────────────────
PREFILTER_LIMIT = 120          # 规则初筛后的候选池（进聚类）
LLM_LIMIT = 40                 # 进 LLM 的条数（一次批量调用）

# ── 规则词表 ────────────────────────────────────────────────────────────
_POLICY_KW = (
    "国务院", "央行", "证监会", "金融监管", "发改委", "工信部", "财政部",
    "商务部", "住建部", "能源局", "部委", "监管", "政策", "指导意见",
    "管理办法", "条例", "规划", "降准", "降息", "LPR", "MLF", "逆回购",
    "专项债", "国债", "补贴", "以旧换新", "税收优惠", "减免", "扶持",
    "配额", "核准", "批复",
)
_OVERSEAS_KW = (
    "美联储", "欧洲央行", "日本央行", "美国", "欧盟", "海外", "关税",
    "出口管制", "制裁", "实体清单", "OPEC", "俄乌", "中东", "纳斯达克",
    "英伟达", "台积电", "三星", "SK海力士", "美光", "全球", "进口",
)
_CAPITAL_KW = (
    "北向", "南向", "融资余额", "两融", "ETF", "增量资金", "主力资金",
    "成交额", "换手率", "公募", "私募", "险资", "社保基金", "回购",
    "增持", "减持", "解禁", "大宗交易", "举牌", "募资",
)
_COMPANY_KW = (
    "公告", "中标", "订单", "合同", "业绩预告", "净利润", "营业收入",
    "并购", "收购", "重组", "停产", "复产", "召回", "立案", "处罚",
    "问询", "退市", "股东大会", "澄清", "股东",
)
_INDUSTRY_KW = (
    "涨价", "跌价", "提价", "报价", "产能", "开工率", "库存", "出货量",
    "排产", "渗透率", "招标", "量产", "投产", "扩产", "技术突破",
    "国产化", "供需", "需求", "供给", "产业链", "渗透",
)

_BULLISH_KW = (
    "涨价", "提价", "上调", "超预期", "中标", "获批", "核准", "突破",
    "扩产", "放量", "增长", "回暖", "利好", "支持", "补贴", "提升",
    "新高", "签订", "量产", "中选", "落地", "加速", "紧缺", "供不应求",
    "订单", "翻倍", "创纪录",
)
_BEARISH_KW = (
    "降价", "下调", "低于预期", "亏损", "下滑", "停产", "召回", "处罚",
    "立案", "退市", "风险警示", "减持", "解禁", "裁员", "取消", "制裁",
    "关税", "限制", "禁止", "过剩", "跌幅", "承压", "萎缩", "终止",
    "违规", "调查", "诉讼", "爆仓",
)

# 例行/无行业指向的噪声（进榜价值低）
_NOISE_KW = (
    "股东大会", "业绩说明会", "投资者关系", "澄清公告", "股票交易异常波动",
    "涨停", "跌停", "龙虎榜", "换届", "人事变动", "辞职",
)
# 值得看的信号词
_SIGNAL_KW = (
    "首次", "首家", "突破", "量产", "投产", "扩产", "中标", "获批",
    "核准", "涨价", "提价", "超预期", "并购", "收购", "重组", "独家",
    "停产", "断供", "国产替代", "出口管制", "配额", "创纪录",
)

_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:亿|万|吨|GW|GWh|MW|%|台|片|辆)")


# ---------------------------------------------------------------------------
# 1. 规则分类 / 打分（无 LLM 也能出完整字段）
# ---------------------------------------------------------------------------


def _count_hits(text: str, words: Iterable[str]) -> int:
    return sum(1 for w in words if w in text)


def classify_event(text: str) -> str:
    """五类事件规则判定（LLM 不可用时的降级路径）。

    按命中词数打分，同分时优先级 overseas > policy > industry > company > capital
    —— 海外与政策是驱动源，industry 是景气变化，公司公告是结果，资金面最末
    （"减持/解禁"这类词同时属于公司行为与资金面，应归公司，否则资金类会虚高）。
    """
    scores = {
        "policy": _count_hits(text, _POLICY_KW),
        "overseas": _count_hits(text, _OVERSEAS_KW),
        "capital": _count_hits(text, _CAPITAL_KW),
        "company": _count_hits(text, _COMPANY_KW),
        "industry": _count_hits(text, _INDUSTRY_KW),
    }
    best = max(scores.values())
    if best == 0:
        return "industry"
    for name in ("overseas", "policy", "industry", "company", "capital"):
        if scores[name] == best:
            return name
    return "industry"


def rule_direction(text: str) -> tuple[str, int]:
    """规则多空方向 + 强度(1~3)。"""
    bull = _count_hits(text, _BULLISH_KW)
    bear = _count_hits(text, _BEARISH_KW)
    if bull == bear:
        return "neutral", 1
    direction = "bullish" if bull > bear else "bearish"
    gap = abs(bull - bear)
    strength = 3 if gap >= 3 else (2 if gap == 2 else 1)
    return direction, strength


def rule_importance(item: dict, event_type: str) -> float:
    """规则重要性 0~10（LLM 不可用时顶替 importance）。"""
    text = f"{item.get('title', '')} {item.get('content', '')}"
    score = 3.0
    if event_type in ("policy", "overseas"):
        score += 2.0
    elif event_type == "industry":
        score += 1.0
    score += min(int(item.get("source_count", 1)) - 1, 3)
    if _count_hits(text, _SIGNAL_KW):
        score += 1.5
    if _NUM_RE.search(text):
        score += 0.5
    if _count_hits(text, _NOISE_KW):
        score -= 2.0
    return round(max(1.0, min(10.0, score)), 2)


def prefilter_score(item: dict) -> float:
    """规则初筛分（决定谁进 LLM，不直接决定排名）。"""
    title = str(item.get("title", ""))
    text = f"{title} {item.get('content', '')}"
    score = 0.0
    score += 2.0 * min(int(item.get("source_count", 1)), 3)   # 多源报道
    score += 1.5 * min(_count_hits(text, _SIGNAL_KW), 3)      # 信号词
    score += 1.0 * min(_count_hits(text, _POLICY_KW), 2)      # 政策属性
    if _NUM_RE.search(text):
        score += 0.8                                          # 有具体数字
    if 10 <= len(title) <= 80:
        score += 0.5
    score -= 2.5 * min(_count_hits(text, _NOISE_KW), 2)       # 例行公告
    return score


# ---------------------------------------------------------------------------
# 2. 事件聚类（同事件多源合并）
# ---------------------------------------------------------------------------


# ── 事件聚类 ────────────────────────────────────────────────────────────
_MIN_CLUSTER_TITLE_LEN = 8     # 规范化标题短于此不参与聚类（字符集太小易误并）
CLUSTER_JACCARD = 0.5


def _char_jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _cluster_id(seed: str) -> str:
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]


def cluster_news(items: list[dict], threshold: float = 0.5) -> list[dict]:
    """按标题字符 Jaccard 聚类，**保留每一条**并为它们打上 cluster_id / source_count。

    不引入 embedding：同一事件的多源转载在字符集上高度重叠（只差标点与
    来源前缀），Jaccard ≥0.5 足以合并；措辞改写的相似新闻不会被误并。

    短标题（规范化后 <8 字）不参与聚类、各自成簇：字符集太小，两条无关的
    短标题（"XX公告"/"XX中标"）Jaccard 也会过 0.5，合并会误删真实新闻。

    这里只"标记"不"去重"：同一簇在排序时最多占一个榜位（见
    ``rank_with_quota``），但簇内成员保留下来 —— 否则同事件被多家转发时
    榜单会缩水成几条（曾经 30 条同簇只落 2 条）。
    """
    from .news_board import normalize_title

    clusters: list[dict] = []
    for item in items:
        key = normalize_title(str(item.get("title", "")))
        if not key:
            continue
        hit = None
        if len(key) >= _MIN_CLUSTER_TITLE_LEN:
            for cl in clusters:
                if cl.get("_short"):
                    continue
                if _char_jaccard(key, cl["_key"]) >= threshold:
                    hit = cl
                    break
        if hit is None:
            clusters.append({
                "_key": key, "_members": [item],
                "_short": len(key) < _MIN_CLUSTER_TITLE_LEN,
            })
        else:
            hit["_members"].append(item)

    out: list[dict] = []
    for cl in clusters:
        members = cl["_members"]
        sources = {str(m.get("source", "")).strip() for m in members if m.get("source")}
        count = max(len(members), len(sources), 1)
        cid = _cluster_id(cl["_key"])
        # 簇内按内容长度降序：信息量最大的一条优先成为榜位代表
        members.sort(key=lambda m: len(str(m.get("content", ""))), reverse=True)
        for m in members:
            row = dict(m)
            row["cluster_id"] = cid
            row["source_count"] = count
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# 3. LLM 批量评估
# ---------------------------------------------------------------------------


class ThemeImpact(BaseModel):
    """单条新闻对某个 L3 主题的影响。"""

    theme: str = ""
    direction: str = "neutral"        # bullish / bearish / neutral
    strength: int = Field(default=1, ge=1, le=5)
    elasticity: float = Field(default=1.0, ge=0.5, le=2.0)
    basis: str = ""


class NewsAssessment(BaseModel):
    """LLM 对单条新闻的评估（相对强度，不给百分比）。"""

    index: int = Field(default=0, ge=0)
    event_type: str = "industry"
    importance: float = Field(default=5.0, ge=0, le=10)
    direction: str = "neutral"
    strength: int = Field(default=1, ge=1, le=5)
    horizon: str = "1d"
    confidence: float = Field(default=0.5, ge=0, le=1)
    themes: list[ThemeImpact] = Field(default_factory=list)
    reasoning: str = ""


class NewsAssessmentReport(BaseModel):
    items: list[NewsAssessment] = Field(default_factory=list)


_catalog_cache: tuple[float, list[str], dict[str, str]] | None = None
_CATALOG_TTL = 60.0


def _theme_catalog() -> tuple[list[str], dict[str, str]]:
    """当前 active 主题名清单 + 别名→主题名映射（LLM 的闭集）。

    进程内缓存 60s：一次落库会对 20~40 条新闻反复调用，逐条查两张表没有必要。
    """
    global _catalog_cache
    import time

    if _catalog_cache and time.time() - _catalog_cache[0] < _CATALOG_TTL:
        return _catalog_cache[1], _catalog_cache[2]
    try:
        from ai_stock.quant.theme_registry import STATUS_ACTIVE, list_themes

        themes = list_themes(STATUS_ACTIVE)
    except Exception as exc:
        logger.debug("theme catalog unavailable: %s", exc)
        return [], {}
    names = [t["name"] for t in themes]
    alias_map: dict[str, str] = {}
    try:
        from sqlalchemy import select

        from ai_stock.quant.db import session_scope
        from ai_stock.quant.db_models import QuantThemeAlias

        with session_scope() as session:
            by_id = {t["id"]: t["name"] for t in themes}
            for row in session.execute(select(QuantThemeAlias)).scalars().all():
                name = by_id.get(row.theme_id)
                if name and row.alias:
                    alias_map[row.alias] = name
    except Exception as exc:
        logger.debug("theme alias load failed: %s", exc)
    for name in names:
        alias_map.setdefault(name, name)
    _catalog_cache = (time.time(), names, alias_map)
    return names, alias_map


def _build_prompt(items: list[dict], theme_names: list[str]) -> str:
    lines = []
    for i, item in enumerate(items):
        content = str(item.get("content", "") or "")[:120]
        lines.append(
            f"[{i}] ({item.get('source', '')} 源数{item.get('source_count', 1)}) "
            f"{str(item.get('title', '')).strip()}：{content}"
        )
    catalog = "、".join(theme_names) if theme_names else "（本次无可用主题，themes 留空）"
    return (
        "你是 A 股事件驱动分析师。下面是一批候选新闻（已去重、已合并同事件多源报道）。"
        "请对**每一条**判断它对 A 股的影响。\n\n"
        "输出字段（每条）：\n"
        "- index：新闻编号（必须与输入一致）\n"
        "- event_type：policy 政策 / industry 行业 / company 公司 / capital 资金 / overseas 海外\n"
        "- importance：0~10，8 以上才是「必看」，无行业指向的例行公告 ≤3\n"
        "- direction：bullish 利好 / bearish 利空 / neutral 中性\n"
        "- strength：影响强度 1~5（1 微弱、3 一般、5 极强）\n"
        "- horizon：影响窗口 1d / 3d / 1w\n"
        "- confidence：0~1，你对该判断的把握\n"
        "- themes：受影响的主题，最多 2 个，每项含 theme/direction/strength/elasticity/basis\n"
        "- reasoning：一句话，必须引用新闻里的事实（金额/产能/时间/部门）\n\n"
        "硬约束：\n"
        f"1) theme 必须从下列清单中原样选择，禁止自创、禁止写「半导体」这类大行业：\n   {catalog}\n"
        "2) 只给相对强度 1~5，**不要给涨跌百分比**（幅度由系统按波动率校准）\n"
        "3) elasticity 是成分股相对主题的弹性，0.5~2.0，龙头小市值给高值\n"
        "4) 新闻与清单里任何主题都不相关时，themes 给空数组，不要硬凑\n"
        "5) 一条新闻只能出现在一个事件里，请全部评估，不要遗漏\n\n"
        "新闻列表：\n" + "\n".join(lines)
    )


def assess(items: list[dict], llm=None) -> dict[int, NewsAssessment]:
    """一次批量 LLM 调用；失败返回空字典（调用方走规则降级）。"""
    if not items:
        return {}
    names, _ = _theme_catalog()
    try:
        from ai_stock.quant.llm_helper import structured_invoke

        report = structured_invoke(
            llm, NewsAssessmentReport,
            _build_prompt(items, names),
            default=NewsAssessmentReport(), deep=False,
        )
    except Exception as exc:
        logger.warning("news assess failed, falling back to rules: %s", exc)
        return {}
    return {
        a.index: a for a in report.items
        if 0 <= a.index < len(items) and (a.event_type or a.themes)
    }


# ---------------------------------------------------------------------------
# 4. 主题对齐（闭集 + 三级匹配）
# ---------------------------------------------------------------------------


def align_themes(
    text: str,
    llm_themes: list[str] | None = None,
    *,
    theme_names: list[str] | None = None,
    alias_map: dict[str, str] | None = None,
    limit: int = 2,
) -> list[str]:
    """把 LLM 给的主题名收敛到 active 集合内，落不到就换关键词/成分股反查。

    三级顺序（对应设计文档 §4.5）：
    1. LLM 输出 ∩ 闭集（别名归一）
    2. ``match_themes`` 别名/关键词匹配
    3. 成分股公司名反查（新闻正文提到"阳光电源"→ 命中其所属主题）
    **不回落到一级行业** —— 落不到主题就返回空。
    """
    if not text and not llm_themes:
        return []
    if theme_names is None or alias_map is None:
        theme_names, alias_map = _theme_catalog()
    allowed = set(theme_names)

    out: list[str] = []
    for raw in llm_themes or []:
        name = alias_map.get(str(raw).strip())
        if name and name in allowed and name not in out:
            out.append(name)
    if len(out) >= limit:
        return out[:limit]

    try:
        from ai_stock.quant.theme_registry import match_themes

        for hit in match_themes(text, limit=limit):
            if hit["name"] in allowed and hit["name"] not in out:
                out.append(hit["name"])
            if len(out) >= limit:
                return out[:limit]
    except Exception as exc:
        logger.debug("match_themes fallback failed: %s", exc)

    for name in _themes_by_stock_name(text, theme_names):
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out[:limit]


# 成分股名称缓存：symbol -> name，一天有效（名称极少变，只为省一次批量请求）
_name_cache: dict[str, str] = {}
_name_cache_at: float = 0.0


def _themes_by_stock_name(text: str, theme_names: list[str]) -> list[str]:
    """新闻正文提及某主题成分股公司名 → 命中该主题（按市值角色排序）。"""
    import time

    global _name_cache, _name_cache_at
    if not text:
        return []
    try:
        from ai_stock.quant.theme_registry import list_themes, theme_members

        if not _name_cache or time.time() - _name_cache_at > 86400:
            symbols: list[str] = []
            for t in list_themes():
                symbols.extend(m["symbol"] for m in theme_members(t["id"]))
            symbols = sorted(set(symbols))
            from ai_stock.dataflows.a_stock import _tencent_quote

            names: dict[str, str] = {}
            for i in range(0, len(symbols), 50):
                for code, row in (_tencent_quote(symbols[i:i + 50]) or {}).items():
                    if row.get("name"):
                        names[code] = str(row["name"])
            if names:
                _name_cache, _name_cache_at = names, time.time()
        allowed = set(theme_names)
        hits: list[tuple[int, str]] = []
        for t in list_themes():
            if t["name"] not in allowed:
                continue
            for m in theme_members(t["id"]):
                nm = _name_cache.get(m["symbol"], "")
                if nm and len(nm) >= 3 and nm in text:   # 3 字以上避免"大唐"类误命中
                    rank = {"leader": 0, "core": 1}.get(m.get("role", ""), 2)
                    hits.append((rank, t["name"]))
                    break
        hits.sort(key=lambda kv: kv[0])
        ordered: list[str] = []
        for _, name in hits:
            if name not in ordered:
                ordered.append(name)
        return ordered
    except Exception as exc:
        logger.debug("stock-name theme lookup failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# 5. 幅度校准（LLM 只给强度，百分比由统计锚定）
# ---------------------------------------------------------------------------


def _sigma_for(theme: str) -> float:
    """主题近 20 日收益率标准差；拿不到退到全部主题中位数，再退到常量。"""
    try:
        from ai_stock.quant.theme_registry import greeks

        sigma = float(greeks(theme).get("sigma20") or 0.0)
        if sigma > 0:
            return sigma
    except Exception as exc:
        logger.debug("theme sigma unavailable for %s: %s", theme, exc)
    return DEFAULT_SIGMA


def market_sigma() -> float:
    """全市场尺度兜底：活跃主题 σ20 的中位数。"""
    try:
        from ai_stock.quant.theme_registry import STATUS_ACTIVE, greeks, list_themes

        sigmas = [
            float(greeks(t["name"]).get("sigma20") or 0.0)
            for t in list_themes(STATUS_ACTIVE)
        ]
        sigmas = [s for s in sigmas if s > 0]
        if sigmas:
            return float(median(sigmas))
    except Exception as exc:
        logger.debug("market sigma unavailable: %s", exc)
    return DEFAULT_SIGMA


def calibrate(
    direction: str, strength: int, elasticity: float,
    sigma: float, horizon: str = "1d",
) -> tuple[float, float]:
    """返回 (预期下限%, 预期上限%)。

    ``幅度 = sign × coef[strength] × σ × elasticity``，区间 [0.6x, 1.4x]，
    单日截断 ±8%、多日 ±15%。

    截断作用在**区间端点**而不是中点：即便模型给了 strength=5 + 弹性 2.0 +
    高波动主题（理论值可到 ±44%），展示出来的上界也不会超过 8%/15%。
    LLM 全程不碰百分比。
    """
    if direction == "neutral":
        return 0.0, 0.0
    strength = max(1, min(5, int(strength or 1)))
    elasticity = max(0.5, min(2.0, float(elasticity or 1.0)))
    sigma = sigma if sigma and sigma > 0 else DEFAULT_SIGMA
    sign = 1.0 if direction == "bullish" else -1.0
    multi = horizon not in ("", "1d")
    cap = CAP_MULTI_DAY if multi else CAP_SINGLE_DAY

    exp = sign * STRENGTH_COEF[strength] * sigma * elasticity
    if multi:
        exp *= HORIZON_FACTOR.get(horizon, 1.0)
    exp = max(-cap, min(cap, exp))

    low, high = exp * 0.6, exp * 1.4
    low = max(-cap, min(cap, low))
    high = max(-cap, min(cap, high))
    return round(min(low, high) * 100, 2), round(max(low, high) * 100, 2)


# ---------------------------------------------------------------------------
# 6. 排序：配额 + 时效 + 多源
# ---------------------------------------------------------------------------


def _freshness(pub_time: str) -> float:
    """0~1 新鲜度权重（48h 线性衰减）。"""
    from datetime import datetime

    try:
        dt = datetime.strptime(str(pub_time)[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return 0.5
    hours = (datetime.now() - dt).total_seconds() / 3600.0
    if hours < 0:
        return 1.0
    return max(0.1, 1.0 - hours / 48.0)


def _rank_key(item: dict) -> tuple[bool, float]:
    """榜单排序键：**能落到主题的排前面**，同组内再比重要性/新鲜度/源数。

    原油暗盘、外盘金价、欧央行表态这类信息价值在"背景"而不在"信号"：它们
    仍然进榜（海外/政策各有保底名额），但不该压住"光模块净利超预期"这种
    能直接对应 L3 主题 + 预期幅度的条目 —— 用户要的是可交易的判断链。
    """
    return (bool(item.get("industry_impacts")), float(item.get("_order") or 0.0))


def rank_with_quota(items: list[dict], top_n: int = 20) -> list[dict]:
    """配额排序选 Top-N 并写 rank。

    每类内部按 ``importance × freshness × min(source_count,3)`` 排序；先满足
    下限保底，再按政策上限填坑，最后用剩余名额补齐 —— 某类不足时名额自动让出。
    """
    scored: dict[str, list[dict]] = {t: [] for t in EVENT_TYPES}
    for item in items:
        etype = item.get("event_type") or "industry"
        if etype not in scored:
            etype = "industry"
            item["event_type"] = etype
        item["_order"] = (
            float(item.get("importance") or 0.0)
            * _freshness(item.get("pub_time", ""))
            * min(int(item.get("source_count", 1)), 3)
        )
        scored[etype].append(item)
    for bucket in scored.values():
        bucket.sort(key=_rank_key, reverse=True)

    picked: list[dict] = []
    seen_clusters: set[str] = set()

    def take(etype: str, count: int) -> None:
        bucket = scored[etype]
        i = 0
        while count > 0 and i < len(bucket):
            item = bucket[i]
            cid = item.get("cluster_id") or ""
            # 同簇（同事件多源转发）最多占一个榜位
            if cid and cid in seen_clusters:
                i += 1
                continue
            # 保底名额不拉低分噪音上位：importance 太低的宁可让位给别的高分条目
            if item["importance"] < QUOTA_MIN_IMPORTANCE:
                i += 1
                continue
            picked.append(bucket.pop(i))
            if cid:
                seen_clusters.add(cid)
            count -= 1

    for etype, minimum in QUOTA_MIN.items():          # 1) 下限保底
        take(etype, minimum)
    take("policy", min(QUOTA_MAX["policy"], top_n - len(picked)))  # 2) 政策上限
    rest = sorted(                                  # 3) 剩余名额按分数补齐
        [x for bucket in scored.values() for x in bucket],
        key=_rank_key, reverse=True,
    )
    counts = {t: 0 for t in EVENT_TYPES}
    for item in picked:
        counts[item["event_type"]] = counts.get(item["event_type"], 0) + 1

    def fill(item: dict) -> None:
        picked.append(item)
        counts[item["event_type"]] = counts.get(item["event_type"], 0) + 1
        cid = item.get("cluster_id") or ""
        if cid:
            seen_clusters.add(cid)

    for item in rest:
        if len(picked) >= top_n:
            break
        if item["importance"] < QUOTA_MIN_IMPORTANCE:
            continue
        cid = item.get("cluster_id") or ""
        if cid and cid in seen_clusters:
            continue
        # 补齐阶段同样严守政策上限，否则高分政策会把剩余名额全吃掉
        if (item["event_type"] == "policy"
                and counts.get("policy", 0) >= QUOTA_MAX["policy"]):
            continue
        fill(item)

    # 4) 榜单连一半都凑不齐（候选池集中在单一类型）→ 放开政策上限，但保持
    #    同簇唯一与重要性门槛；宁可政策多几条，也不让用户看到半张空表。
    if len(picked) < top_n / 2:
        for item in rest:
            if len(picked) >= top_n:
                break
            if item["importance"] < QUOTA_MIN_IMPORTANCE:
                continue
            cid = item.get("cluster_id") or ""
            if cid and cid in seen_clusters:
                continue
            fill(item)

    # 5) 兜底：一条都没选出来（全是低分噪音）→ 按分数取，避免完全空榜
    if not picked:
        picked.extend(rest[:top_n])
    picked = picked[:top_n]

    picked.sort(key=_rank_key, reverse=True)
    for i, item in enumerate(picked, 1):
        item["rank"] = i
    return picked


# ---------------------------------------------------------------------------
# 7. 主入口
# ---------------------------------------------------------------------------


def prepare(news_items: list[dict], limit: int = PREFILTER_LIMIT) -> list[dict]:
    """去重 → 规则初筛 → 聚类 → 规则字段，返回候选池（不调 LLM）。

    这一步很便宜（纯本地），用来判断"值不值得调 LLM"以及做快照节流指纹。
    """
    from .news_board import dedup_news

    valid = [
        dict(n) for n in (news_items or [])
        if str(n.get("title", "")).strip()
    ]
    if not valid:
        return []
    valid.sort(key=lambda n: (prefilter_score(n), str(n.get("time", ""))), reverse=True)
    # 先按规范化标题去重（同一条新闻的标点/前缀差异），再做事件聚类（措辞不同
    # 但字符高度重叠的多源报道）。两步都做，因为前者精确、后者召回更宽。
    valid = dedup_news(valid)[:limit]
    clustered = cluster_news(valid)

    for item in clustered:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        etype = item.get("event_type") or classify_event(text)
        direction, strength = rule_direction(text)
        item["event_type"] = etype
        item["category"] = "policy" if etype == "policy" else "news"
        item["direction"] = direction
        item["strength"] = strength
        item["importance"] = item.get("importance") or rule_importance(item, etype)
        item["horizon"] = item.get("horizon") or "1d"
        item["confidence"] = item.get("confidence") or 0.35   # 规则阶段低置信
        item["themes"] = item.get("themes") or []
    clustered.sort(
        key=lambda n: (n["importance"], str(n.get("time", ""))), reverse=True
    )
    return clustered


def _rule_impacts(item: dict, text: str) -> list[dict]:
    """规则降级：用 match_themes / 成分股反查拿主题，幅度按规则强度校准。"""
    themes = align_themes(text, item.get("themes"), limit=2)
    impacts: list[dict] = []
    for name in themes:
        low, high = calibrate(
            item.get("direction", "neutral"), item.get("strength", 1),
            1.0, _sigma_for(name), item.get("horizon", "1d"),
        )
        impacts.append({
            "name": name,
            "direction": item.get("direction", "neutral"),
            "strength": item.get("strength", 1),
            "elasticity": 1.0,
            "exp_low": low,
            "exp_high": high,
            "basis": "规则匹配（LLM 未参与）",
        })
    return impacts


def enrich(prepared: list[dict], llm=None, *, llm_limit: int = LLM_LIMIT) -> list[dict]:
    """给候选池补事件类型/重要性/多空/主题/幅度。

    有 LLM 时：前 ``llm_limit`` 条走批量评估，其余用规则；LLM 失败整体降级。
    返回的每条含 ``industry_impacts``（list[dict]）与兼容旧字段的
    ``industries`` / ``expected_gain_low/high`` / ``bull_bear_bias``。
    """
    if not prepared:
        return []
    assessed = assess(prepared[:llm_limit], llm) if llm is not None else {}

    out: list[dict] = []
    for idx, item in enumerate(prepared):
        text = f"{item.get('title', '')} {item.get('content', '')}"
        a = assessed.get(idx)
        if a is not None:
            item["event_type"] = a.event_type if a.event_type in EVENT_TYPES else item["event_type"]
            item["category"] = "policy" if item["event_type"] == "policy" else "news"
            item["importance"] = round(float(a.importance), 2)
            item["direction"] = a.direction
            item["strength"] = a.strength
            item["horizon"] = a.horizon if a.horizon in HORIZON_FACTOR else "1d"
            item["confidence"] = round(float(a.confidence), 2)
            item["reasoning"] = a.reasoning

            chosen = [t.theme for t in a.themes if t.theme]
            aligned = align_themes(text, chosen, limit=2)
            impacts = []
            for name in aligned:
                src = next((t for t in a.themes if
                            (t.theme == name or t.theme in name)), None)
                direction = (src.direction if src and src.direction in
                             ("bullish", "bearish", "neutral") else a.direction)
                strength = src.strength if src else a.strength
                elasticity = src.elasticity if src else 1.0
                low, high = calibrate(
                    direction, strength, elasticity, _sigma_for(name), item["horizon"],
                )
                impacts.append({
                    "name": name,
                    "direction": direction,
                    "strength": strength,
                    "elasticity": round(elasticity, 2),
                    "exp_low": low,
                    "exp_high": high,
                    "basis": (src.basis if src and src.basis else a.reasoning or "")[:120],
                })
            if not impacts:      # LLM 没选中任何主题 → 关键词/成分股反查兜底
                impacts = _rule_impacts(item, text)
        else:
            impacts = _rule_impacts(item, text)

        item["industry_impacts"] = impacts
        item["industries"] = [i["name"] for i in impacts]
        primary = impacts[0] if impacts else None
        # 落不到主题时一律中性：\"利空\"没有承受对象就是噪音（如外盘金价下跌），
        # 榜单上给个方向箭头却指不出行业，比不给更糟。
        item["bull_bear_bias"] = primary["direction"] if primary else "neutral"
        item["bias_strength"] = int(primary["strength"]) if primary else 0
        item["expected_gain_low"] = primary["exp_low"] if primary else 0.0
        item["expected_gain_high"] = primary["exp_high"] if primary else 0.0
        item["top_stocks"] = _top_stocks(primary["name"]) if primary else []
        out.append(item)
    return out


_stock_cache: dict[str, list[dict]] = {}


def _top_stocks(theme: str) -> list[dict]:
    """主题的弹性个股（取角色为 leader 的前 2 只，带名称）。"""
    if theme in _stock_cache:
        return _stock_cache[theme]
    try:
        from ai_stock.quant.theme_registry import get_theme, theme_members

        t = get_theme(theme)
        if not t:
            return []
        members = [m for m in theme_members(t["id"]) if m.get("role") == "leader"]
        if not members:
            members = theme_members(t["id"])[:2]
        symbols = [m["symbol"] for m in members[:2]]
        from ai_stock.dataflows.a_stock import _tencent_quote

        names = {c: r.get("name", "") for c, r in (_tencent_quote(symbols) or {}).items()}
        out = [
            {"code": s, "name": names.get(s, ""), "elasticity": 1.2 if i == 0 else 1.0}
            for i, s in enumerate(symbols)
        ]
        _stock_cache[theme] = out
        return out
    except Exception as exc:
        logger.debug("top stocks lookup failed for %s: %s", theme, exc)
        return []


def fingerprint(prepared: list[dict], top_n: int = 40) -> set[str]:
    """榜单内容指纹（标题集合），供快照节流做集合差异比较。"""
    return {str(i.get("title", "")).strip() for i in prepared[:top_n] if i.get("title")}


def content_diff(a: set[str], b: set[str]) -> float:
    """两份指纹的差异度（0=完全包含，1=毫无交集）。

    用 ``交集 / min(两边条数)`` 而不是 Jaccard：两边条数本来就不等
    （当前候选池 40 条 vs 上期榜单 20 条），Jaccard 会把"榜单完整包含在
    候选池里"算成 0.5 的差异，节流永远不生效。这里的语义是
    **上期榜单有多少比例还留在当前候选池** —— 那才是"内容有没有变"。
    """
    if not a or not b:
        return 1.0
    return 1.0 - len(a & b) / max(1, min(len(a), len(b)))
