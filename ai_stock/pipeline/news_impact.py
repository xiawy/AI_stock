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
import os
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
# 单次结构化调用的条数上限。40 条一次性送出时，模型输出（每条约 200~300 字
# 的 reasoning + basis）会超过 max_tokens 被截断，JSON 不完整 → 整批降级成
# 规则，日志只有一句"0 llm"（2026-10-19 快照 981 实测 40 条全丢）。分批后
# 一批失败最多影响 12 条，且失败批还能再对半重试。
# 单批条数：12 条时生产实测仍偶发输出截断（快照 982 有一批 12 条整批失败，
# 降到 6+6 才对）。8 条是这个模型稳定通过的上限。
ASSESS_BATCH = int(os.getenv("NEWS_ASSESS_BATCH", "8"))

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

# 仪式性新闻：有活动、无结果。提示词对 LLM 明确要求这类不给主题，
# 规则降级路径必须遵守同一口径，否则「中国航展海上装备展示区将带来全新体验」
# 会被关键词匹配到「机器人（关节模组）」——同一个口径不能只有 LLM 守。
_CEREMONIAL_KW = (
    "举办", "召开", "开幕", "闭幕", "揭牌", "签约仪式", "启动仪式",
    "论坛", "峰会", "发布会", "致辞", "参观", "调研", "展示区",
    "带来全新体验", "亮相", "交流会", "座谈会", "圆桌",
)


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
    """规则初筛分（决定谁进 LLM，不直接决定排名）。

    ``_theme_score`` 由 :func:`prepare` 批量预计算写入：命中 L3 主题的新闻是
    **唯一能给出行业 + 幅度**的那类，必须优先进入 LLM 窗口。生产实测（10-19）
    候选池里 6 条命中主题、其中 5 条排在第 40 名之后 —— 它们连被 LLM 看一眼的
    机会都没有，榜单自然只剩政策噪音。
    """
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
    # 主题命中：别名级（≥2.5）给足权重，让它压过泛泛的政策稿
    hint = float(item.get("_theme_score") or 0.0)
    if hint >= 2.5:
        score += 3.0
    elif hint > 0:
        score += 1.5
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
        "- reasoning：一句话 ≤50 字，必须引用新闻里的事实（金额/产能/时间/部门）\n"
        "输出务必精简：basis ≤50 字只写链条箭头，不要解释、不要复述原文。\n\n"
        "**正例**（这类必须给主题，别留空）：\n"
        "  涨价：DRAM 现货价环比涨 12%、原厂暂停报价 → 半导体（存储涨价），bullish/4，"
        "basis「存储涨价→模组厂采购价上行→毛利率提升」\n"
        "  招标：国网第三批特高压设备招标落地 87 亿元 → 电网（特高压），bullish/4，"
        "basis「招标落地→换流阀/主设备订单→设备商在手订单增」\n"
        "  扩产：某存储厂追加 200 亿资本开支建厂 → 半导体（设备零部件），bullish/3，"
        "basis「扩产→刻蚀/薄膜设备采购→设备与零部件订单增」\n"
        "  政策：多地出台 BC 电池组件补贴细则 → 光伏（BC电池），bullish/3，"
        "basis「补贴→BC 组件需求与溢价→电池厂出货与毛利提升」\n"
        "**反例**（这类必须留空）：展会/论坛/揭牌/签约仪式；外盘金价油价汇率本身；"
        "主体是海外公司且 A 股无对标环节；只能说出「受益于某大行业景气」而说不出环节的。\n\n"
        "硬约束：\n"
        f"1) theme 必须从下列清单中原样选择，禁止自创、禁止写「半导体」这类大行业：\n   {catalog}\n"
        "2) 只给相对强度 1~5，**不要给涨跌百分比**（幅度由系统按波动率校准）\n"
        "3) elasticity 是**成分股收入对该驱动的暴露度**（营收占比 × 量价弹性），0.5~2.0；"
        "不要用它表达「我觉得这事重要」—— 重要程度归 strength。链条里有 2 个以上箭头的"
        "间接传导，strength 与 elasticity 都不得高于 2.0/1.0\n"
        "4) 只有存在**指向该主题成分股自身收入/订单**的传导链才给 theme。合规的链条有四类："
        "产品价格上涨→生产商毛利；招标中标→施工/设备单位在手订单；下游大厂扩产→设备与"
        "零部件订单；政策改变支付端或供给端（医保/补贴/出口管制/配额）→量价变化→公司收入。"
        "下列情况 themes 必须给空数组：\n"
        "   - 海外商品或汇率利率的价格波动本身（原油、黄金、铜、美元指数、美债收益率），"
        "除非新闻明确指向国内某个细分品种的供需；\n"
        "   - 展会、论坛、揭牌、签约仪式等**没有量化结果**的仪式性新闻；\n"
        "   - 主体是海外公司/海外政策，且 A 股没有直接对标环节；\n"
        "   - 你只能说出「受益于某个大行业景气」而说不出具体环节的。\n"
        "   一条主题都不给是合法输出，但**不要因为懒得写 basis 就留空** —— 上面正例那四类"
        "明确够格的，必须给。榜单上一条错误的行业映射，比一条空映射代价大；但一条本可以"
        "映射上的涨价/招标/扩产新闻空着，同样是失败。\n"
        "5) 每个 theme 的 basis 必须写出传导链条（如「HBM 需求↑→存储涨价→模组厂毛利↑」）；"
        "写不出链条就不要给这个 theme。basis 里一旦出现「不构成/无直接对应/仅为股权投资/"
        "链条偏弱/无 A 股订单」这类否定表述，就说明你自己也认为没有链条，必须删掉该 theme\n"
        "6) 主题名括号里的限定词是**准入条件**，不是修饰语：选「建筑（水利投资）」要求新闻主线"
        "是水利/水网/防洪，轨道交通、公路、机场、市政景观一律留空；选「电网（特高压）」要求"
        "出现特高压/直流输电/换流阀；选「风电（出海）」要求是海上风电或海外订单。限定词对不上，"
        "宁可换一个能对上的主题，或者留空\n"
        "7) 主题定义的是**驱动因子**，不是**产品构成**。产品里用了同类零部件不等于属于该主题 ——"
        "外骨骼康复机器人、工业机器人、扫地机都装减速器，但驱动它们的分别是医保支付、制造业"
        "资本开支、消费需求，不是「人形机器人量产」。只有新闻主线**本身就是该主题的驱动因子**"
        "（人形机器人的量产/定点/降本/出货量）才能挂；「下游整机放量→零部件间接受益」算间接"
        "传导：strength 最多 2、elasticity 最多 1.0；链条超过 3 跳就别给了\n"
        "8) 一条新闻只能出现在一个事件里，请全部评估，不要遗漏\n\n"
        "新闻列表：\n" + "\n".join(lines)
    )


def assess(items: list[dict], llm=None) -> dict[int, NewsAssessment]:
    """分批批量 LLM 调用；失败批次对半重试一次，仍失败则该批走规则。

    返回 ``{传入列表下标: NewsAssessment}``。**必须分批**：40 条一次送出时
    输出会超 max_tokens 被截断，整批静默降级（快照 981 的 40 条全规则票）。
    """
    if not items:
        return {}
    out: dict[int, NewsAssessment] = {}
    for start in range(0, len(items), ASSESS_BATCH):
        chunk = items[start:start + ASSESS_BATCH]
        part = _assess_chunk(chunk, llm)
        if not part and len(chunk) > 1:
            # 整批解析失败 → 对半再试一次（更可能是输出截断，缩小规模即可）
            mid = len(chunk) // 2
            for off, sub in ((0, chunk[:mid]), (mid, chunk[mid:])):
                for local, a in _assess_chunk(sub, llm).items():
                    part[off + local] = a
            logger.warning(
                "news assess: batch of %d failed, retried as %d+%d → %d ok",
                len(chunk), mid, len(chunk) - mid, len(part),
            )
        for local, a in part.items():
            out[start + local] = a
    return out


def _assess_chunk(items: list[dict], llm=None) -> dict[int, NewsAssessment]:
    """一次结构化调用（≤ ``ASSESS_BATCH`` 条）；失败返回空字典。"""
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
    got = {
        a.index: a for a in report.items
        if 0 <= a.index < len(items) and (a.event_type or a.themes)
    }
    if not got:
        logger.warning(
            "news assess returned 0 usable items for %d inputs "
            "(structured output truncated or unparsable)", len(items),
        )
    return got


# ---------------------------------------------------------------------------
# 3b. 评估结果缓存
# ---------------------------------------------------------------------------
# 口径变了必须改版本号，否则旧口径的结论会被一直复用 —— 这是缓存最容易出事
# 的地方（改了提示词却看不到效果，因为全命中旧 key）。
# v5：加"驱动因子 ≠ 产品构成"、elasticity 语义收紧、间接传导限强度
#     （2026-10-18 外骨骼医保 → 机器人关节模组 +8% 之后）。
# v6：v5 矫枉过正（主题率 26%→18%，政策密集日整榜空行业），补正例与政策类
#     准入；"间接传导"改为强度封顶而非丢弃（2026-10-19 快照 980 全空之后）。
PROMPT_VERSION = "6"
CACHE_TTL_DAYS = 7


def _cache_key(title: str) -> str:
    import hashlib

    from .news_board import normalize_title

    norm = normalize_title(title) or str(title or "").strip()
    if not norm:
        return ""
    return hashlib.sha1(f"{PROMPT_VERSION}|{norm}".encode("utf-8")).hexdigest()[:32]


def _utcnow_naive():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def title_digest(text_: str) -> str:
    import hashlib

    return hashlib.md5(str(text_ or "").encode("utf-8")).hexdigest()


def load_cached(items: list[dict], *, ttl_days: int = CACHE_TTL_DAYS) -> dict[int, NewsAssessment]:
    """按标题批量查缓存，返回 ``{列表下标: NewsAssessment}``。

    任何异常都返回空字典 —— 缓存是加速手段，不能成为落库的故障点。
    """
    if not items:
        return {}
    keys = [_cache_key(str(it.get("title", ""))) for it in items]
    wanted = {k: i for i, k in enumerate(keys) if k}
    if not wanted:
        return {}
    from datetime import timedelta

    try:
        from sqlalchemy import bindparam, text

        from .db_ops import _get_session

        stmt = text(
            "SELECT cache_key, payload_json FROM news_assess_cache "
            "WHERE cache_key IN :keys AND created_at >= :cutoff"
        ).bindparams(bindparam("keys", expanding=True))
        cutoff = _utcnow_naive() - timedelta(days=ttl_days)
        session = _get_session()
        try:
            rows = session.execute(
                stmt, {"keys": list(wanted), "cutoff": cutoff}
            ).fetchall()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("news assess cache read skipped: %s", exc)
        return {}

    out: dict[int, NewsAssessment] = {}
    for key, payload in rows:
        try:
            model = NewsAssessment.model_validate_json(payload or "{}")
        except Exception:
            continue
        model.index = 0
        out[wanted[key]] = model
    if out:
        logger.info("news assess cache hit %d/%d", len(out), len(items))
    return out


def save_cached(items: list[dict], assessed: dict[int, NewsAssessment], *, model: str = "") -> int:
    """把本轮新评估写回缓存（同 key 覆盖，刷新时间戳）。"""
    if not items or not assessed:
        return 0
    rows: list[dict] = []
    for idx, a in assessed.items():
        if not (0 <= idx < len(items)):
            continue
        title = str(items[idx].get("title", ""))
        key = _cache_key(title)
        if not key:
            continue
        a.index = 0
        rows.append({
            "k": key,
            "h": title_digest(title),
            "t": title[:500],
            "v": PROMPT_VERSION,
            "p": a.model_dump_json(),
            "m": model[:60],
            "now": _utcnow_naive(),
        })
    if not rows:
        return 0
    try:
        from sqlalchemy import text

        from .db_ops import _get_session

        session = _get_session()
        try:
            session.execute(
                text(
                    "INSERT OR REPLACE INTO news_assess_cache "
                    "(cache_key, title_hash, title, prompt_version, payload_json, model, created_at) "
                    "VALUES (:k, :h, :t, :v, :p, :m, :now)"
                ),
                rows,
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("news assess cache write skipped: %s", exc)
        return 0
    return len(rows)


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
    allow_match: bool = True,
    min_score: float = 2.0,
) -> list[str]:
    """把 LLM 给的主题名收敛到 active 集合内，落不到就换关键词/成分股反查。

    三级顺序（对应设计文档 §4.5）：
    1. LLM 输出 ∩ 闭集（别名归一）
    2. ``match_themes`` 别名/关键词匹配
    3. 成分股公司名反查（新闻正文提到"阳光电源"→ 命中其所属主题）
    **不回落到一级行业** —— 落不到主题就返回空。

    ``allow_match=False`` 时只做第 1 步。调用方在 **LLM 判定"没有传导链"**
    （``themes`` 给空数组）时必须关掉兜底：实测「AI 短剧出海」会被关键词
    "出海"匹配到「风电（出海）」、「原油暗盘突破」会被匹配到「油服」并顶到
    榜首 —— 规则能匹配上的词，恰恰是 LLM 刚刚否掉的那条新闻。

    ``min_score`` 默认 **2.0**（一个别名或两个关键词）。LLM 已经给了主题时也
    不放松：那时第 2 步只是"给第二个空位补位"，1 分的关键词补进来的往往是
    泛词误配 —— 「AI 短剧出海」= 传媒（AI应用，LLM 给的）+ 风电（出海，靠
    "出海"补的）就是这么来的。
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
    if not allow_match:
        return out[:limit]

    try:
        from ai_stock.quant.theme_registry import match_themes

        for hit in match_themes(text, limit=limit, min_score=min_score):
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


def _pub_time(item: dict) -> str:
    """取发布时间：源数据用 ``time``，落库后用 ``pub_time``。

    这两个键在链路里并存（``prepare`` 保留原始 ``time``，``_to_news_row`` 才
    映射成 ``pub_time``）。排序直接读 ``pub_time`` 会让新鲜度恒为 0.5 ——
    三天前的旧闻和刚出的消息同权，实测确实如此。
    """
    return str(item.get("pub_time") or item.get("time") or "")


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


def _tradable(item: dict) -> bool:
    """有没有**可交易**的主题：strength≥2 才算。

    ``strength=1`` 的预期幅度约 ±1%，和"没有主题"在决策价值上等价，却因为
    ``bool(industry_impacts)`` 的置顶规则压在榜首。线上原样：外骨骼医保
    （3 跳、自述"弹性存疑"）连续三个快照第 1 位 —— 排序在奖励"找到主题"，
    而不是"找对主题"，LLM 也就是在这个激励下学会凑主题的。
    """
    return any(int(i.get("strength") or 0) >= 2 for i in (item.get("industry_impacts") or []))


def _rank_key(item: dict) -> tuple[bool, float]:
    """榜单排序键：**能落到可交易主题的排前面**，同组内再比重要性/新鲜度/源数。

    原油暗盘、外盘金价、欧央行表态这类信息价值在"背景"而不在"信号"：它们
    仍然进榜（海外/政策各有保底名额），但不该压住"光模块净利超预期"这种
    能直接对应 L3 主题 + 预期幅度的条目 —— 用户要的是可交易的判断链。
    """
    return (_tradable(item), float(item.get("_order") or 0.0))


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
            * _freshness(_pub_time(item))
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
    # 主题命中要先算出来，初筛分才有得用：否则「能不能给出行业」这个最关键
    # 的信号完全不参与"谁进 LLM"的竞争。一次会话批量打分，几百条也只有几毫秒。
    try:
        from ai_stock.quant.theme_registry import match_themes_many

        hits = match_themes_many(
            [f"{n.get('title', '')} {n.get('content', '') or ''}" for n in valid],
            limit=1, min_score=2.0,
        )
        for node, hit in zip(valid, hits):
            if hit:
                node["_theme_score"] = float(hit[0]["score"])
    except Exception as exc:      # 主题库不可用不该拦住新闻榜
        logger.debug("theme prefilter hint skipped: %s", exc)
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


def is_ceremonial(text: str) -> bool:
    """活动报道而非结果：命中仪式性词且**没有任何量化结果**。"""
    if _NUM_RE.search(text or ""):
        return False
    return _count_hits(text or "", _CEREMONIAL_KW) > 0


def _rule_impacts(item: dict, text: str, *, basis: str = "规则匹配（LLM 未参与）") -> list[dict]:
    """规则降级：用 match_themes / 成分股反查拿主题，幅度按规则强度校准。

    两道保守约束（与 LLM 的口径对齐）：
    - 仪式性新闻不给主题（有活动无结果，谈不上传导链）
    - 关键词命中门槛 2 分：单个泛词不算数，至少一个别名或两个关键词
    - 规则判不出方向（neutral）时不给主题：没有方向的"传导链"和"无主题"
      在展示上等价，但前者会让人以为我们判断过 —— 「布伦特原油升至 110
      美元」这类只描述价格本身的新闻就属于此列
    """
    if item.get("direction", "neutral") == "neutral":
        return []
    if is_ceremonial(text):
        return []
    themes = align_themes(text, item.get("themes"), limit=2, min_score=2.0)
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
            "basis": basis,
        })
    return impacts


def enrich(
    prepared: list[dict], llm=None, *, llm_limit: int = LLM_LIMIT,
    use_cache: bool = True,
) -> list[dict]:
    """给候选池补事件类型/重要性/多空/主题/幅度。

    有 LLM 时：前 ``llm_limit`` 条走批量评估（先查缓存，只把未命中的送去
    LLM），其余用规则；LLM 失败整体降级。返回的每条含 ``industry_impacts``
    （list[dict]）与兼容旧字段的 ``industries`` / ``expected_gain_low/high``
    / ``bull_bear_bias``。
    """
    if not prepared:
        return []
    assessed: dict[int, NewsAssessment] = {}
    if llm is not None:
        head = prepared[:llm_limit]
        assessed = load_cached(head) if use_cache else {}
        missing = [i for i in range(len(head)) if i not in assessed]
        mapped: dict[int, NewsAssessment] = {}
        if missing:
            fresh = assess([head[i] for i in missing], llm)
            # assess 返回的下标是子集内序号，映射回 head 的下标
            mapped = {
                missing[local]: a for local, a in fresh.items()
                if 0 <= local < len(missing)
            }
            if mapped:
                save_cached(head, mapped, model=type(llm).__name__)
                assessed.update(mapped)
            logger.info(
                "news assess: %d items, %d cache hit, %d llm, %d rule",
                len(head), len(head) - len(missing), len(mapped), len(missing) - len(mapped),
            )

    out: list[dict] = []
    for idx, item in enumerate(prepared):
        a = assessed.get(idx)
        if a is not None:
            _apply_assessment(item, a)
            item["assess_source"] = "llm"
        else:
            _apply_rule(item)
            item["assess_source"] = "rule"
        out.append(item)
    return out


def _finish_impacts(item: dict, impacts: list[dict]) -> None:
    """主题列表 → 条目上的行业/多空/幅度/个股字段（LLM 与规则共用）。

    **多空与行业解耦**：没有落到 L3 主题 ≠ 没有方向。「证监会支持外资来华」
    对市场是利好，只是说不出受益的是哪个细分赛道。原来的实现把两者绑死
    （无主题 → neutral），导致政策密集日整张榜单多空列全是"中性"，用户看到
    的是"什么都没判断"。

    现在分两级，用 ``bias_scope`` 标明方向的作用域：

    - ``theme``：方向针对具体 L3 主题，强度按主题给（"强多"）；
    - ``market``：方向只针对市场整体/情绪，强度封顶 1（"弱多"），前端需
      标注"整体"以示区别 —— 这一格不能让人误读成"某个行业强多"。
    """
    item["industry_impacts"] = impacts
    item["industries"] = [i["name"] for i in impacts]
    primary = impacts[0] if impacts else None
    if primary:
        item["bull_bear_bias"] = primary["direction"]
        item["bias_strength"] = int(primary["strength"])
        item["bias_scope"] = "theme"
    else:
        news_dir = item.get("direction") or "neutral"
        # 只有 LLM 评估过的条目才给"整体方向"：规则方向靠关键词计数
        # （"支持/提升/增长"在政策稿里满地都是），拿它当市场方向会误导。
        if news_dir in ("bullish", "bearish") and item.get("assess_source") == "llm":
            item["bull_bear_bias"] = news_dir
            item["bias_strength"] = 1     # 整体级方向不给强度，避免"空主题显示强多"
            item["bias_scope"] = "market"
        else:
            item["bull_bear_bias"] = "neutral"
            item["bias_strength"] = 0
            item["bias_scope"] = ""
    # 幅度只能基于具体标的，没有主题就没有百分比 —— 这一列留空是对的，
    # 与"多空"不同：方向可以只对市场，涨幅必须对行业。
    item["expected_gain_low"] = primary["exp_low"] if primary else 0.0
    item["expected_gain_high"] = primary["exp_high"] if primary else 0.0
    item["top_stocks"] = _top_stocks(primary["name"]) if primary else []


# 依据里自己写了"没有传导链"的，主题不成立：LLM 有时为了凑满 slot 硬塞一个，
# 再在 basis 里否定它。线上原样（快照 974）：
#   「综合交通规划含轨道 1050 公里目标→…，但水利方向无直接对应，链条偏弱」
#   「长鑫科技参与…仅为股权投资行为，不构成存储供需或价格的传导链」
#   「…为一级市场融资，无 A 股域控供应商订单落地」
_CHAIN_NEGATION = (
    "不构成", "无直接", "并非", "谈不上", "链条偏弱", "无A股", "无 A 股",
    "仅为股权", "不形成", "不受益", "难言",
)
# 双重否定不能误伤：basis 说"不构成利空"其实是给了方向
_CHAIN_NEGATION_EXEMPT = ("不构成利空", "不构成利好", "不构成明显利空", "不构成明显利好")


def _basis_negates_chain(basis: str) -> bool:
    text_ = basis or ""
    if any(x in text_ for x in _CHAIN_NEGATION_EXEMPT):
        return False
    return any(x in text_ for x in _CHAIN_NEGATION)


# 「承认链条存在、但否定它的量级」——与 _CHAIN_NEGATION（否定链条存在）不同：
# 这类不该直接丢（真有传导），但也不能按一级受益给强度。
# 线上原样（快照 974 reasoning）：「与清单主题仅部分重叠，弹性存疑」。
_CHAIN_WEAK = (
    "弹性存疑", "仅部分重叠", "部分重叠", "有限", "弱相关",
    "概念性", "传导偏长", "影响有限", "难以量化", "象征性", "帮助不大",
)
# 「间接传导」不是自我否定，只是量级降档。
# 提示词第 7 条**要求**间接传导在 basis 里写明「间接」；如果这里把"间接"
# 也算作自述弱词并丢弃，就等于"谁守指令谁被判死刑"——2026-10-19 快照 980
# 全榜空行业，2 条本可保留的主题（台积电 3nm 设备、资本市场助力人形机器人）
# 都是这么没的。所以单独一档：保留主题、封顶 strength=2、elasticity≤1.0。
_CHAIN_INDIRECT = ("间接", "情绪传导", "仅情绪", "间接受益", "映射")
_ARROW_TOKENS = ("→", "->", "=>", "⇒")


def _transmission_hops(basis: str) -> int:
    """basis 里的箭头数 = 传导级数（``A→B`` 是 1 跳）。"""
    t = basis or ""
    return sum(t.count(x) for x in _ARROW_TOKENS)


def _theme_evidence(text: str, theme_names: list[str]) -> set[str] | None:
    """正文里**确实提到了**该主题的证据：专指词 1 分即算，或点名成分股。

    只认专指词（``min_score=1.0``；泛词在 ``match_themes`` 里记 0 分，不会被
    算进来），所以「外骨骼医保」这种正文里一个"关节模组/减速器/丝杠"都没有的
    新闻，拿不到机器人主题的证据。

    返回 ``None`` 表示**判断不了**（关键词那一路取数异常）—— 调用方据此不降权，
    宁可放过也不要因为一次 DB 抖动把正确主题全降成微弱。成分股名反查要联网，
    失败只是少一路证据（关键词那一路已经够用），不会让整个判定失效。
    """
    from ai_stock.quant.theme_registry import match_themes

    if not text:
        return set()
    try:
        names = {h["name"] for h in match_themes(text, limit=8, min_score=1.0)}
    except Exception as exc:
        logger.debug("theme evidence (keywords) unavailable: %s", exc)
        return None
    try:
        names.update(_themes_by_stock_name(text, theme_names))
    except Exception as exc:
        logger.debug("theme evidence (stock names) unavailable: %s", exc)
    return names


def _temper_impact(
    strength: int, elasticity: float, basis: str, reasoning: str = "",
    in_universe: bool = True,
) -> tuple[bool, int, float]:
    """按传导级数与自述强弱修正强度；返回 ``(是否保留, strength, elasticity)``。

    主题的本意是「一组股票被同一个驱动因子驱动」，链条越长这个前提越不成立。
    线上原样（快照 975 第 1 位，+3.4~8.0%）：

        医保规划→纳入多地医保→整机采购放量→关节模组需求↑（3 跳）

    外骨骼整机年出货万台级，减速器采购额对双环/绿的的收入贡献不到 1%，却因为
    σ20 用的是人形机器人主题的 4%，被算成接近单日上限的涨幅 —— 驱动因子根本
    不是同一个。

    但**只看箭头数会误伤**：LLM 写链条爱堆箭头，「光迅科技净利超预期→800G 需求
    确认→上游光模块受益」也有 3 个箭头，而光迅本身就是成分股，这是一级驱动。
    「混合键合设备→北方华创平台化→上游零部件」同理，它命中了「设备零部件」，
    属于半导体产业链内部延伸。

    所以"链条长"只有在**新闻整条落在主题体系之外**（任何主题的关键词/成分股都
    没命中）时才算问题 —— 那才是纯粹的跨行业联想，比如「外骨骼医保→关节模组」。
    四档：

    - 自述弱（"弹性存疑/仅部分重叠"）且 ≥2 跳 → 丢弃，不构成可交易映射；
    - 落在主题体系外且 ≥3 跳 → 保留但压到 strength=1、elasticity≤1.0（约 ±1%）；
    - 自述弱（无论几跳）→ 同样压强度；
    - 自述"间接"且 ≥2 跳 → **保留**，strength 封顶 2、elasticity≤1.0
      （间接传导最多给"中多/中空"，不按一级驱动给满）；
    - 其余不动（含"光迅业绩超预期"这类成分股自身的一级驱动）。
    """
    hops = _transmission_hops(basis)
    blob = f"{basis} {reasoning}"
    weak = any(x in blob for x in _CHAIN_WEAK)
    indirect = any(x in blob for x in _CHAIN_INDIRECT)
    if weak and hops >= 2:
        return False, strength, elasticity
    if weak or (not in_universe and hops >= 3):
        return True, min(int(strength or 1), 1), min(float(elasticity or 1.0), 1.0)
    if indirect and hops >= 2:
        return True, min(int(strength or 1), 2), min(float(elasticity or 1.0), 1.0)
    return True, strength, elasticity


def _apply_assessment(item: dict, a: NewsAssessment) -> None:
    """把一次 LLM 评估写进条目（事件类型/重要性/主题归一/幅度校准）。"""
    text = f"{item.get('title', '')} {item.get('content', '')}"
    # 先打来源：_finish_impacts 里"无主题是否给整体方向"要用它区分 LLM / 规则
    item["assess_source"] = "llm"
    item["event_type"] = a.event_type if a.event_type in EVENT_TYPES else item["event_type"]
    item["category"] = "policy" if item["event_type"] == "policy" else "news"
    item["importance"] = round(float(a.importance), 2)
    item["direction"] = a.direction
    item["strength"] = a.strength
    item["horizon"] = a.horizon if a.horizon in HORIZON_FACTOR else "1d"
    item["confidence"] = round(float(a.confidence), 2)
    item["reasoning"] = a.reasoning

    chosen = [t.theme for t in a.themes if t.theme]
    # LLM 给了主题名才允许关键词归一；给了空数组说明它判定"没有传导链"，
    # 这时再让规则匹配等于推翻它的判断（实测会把「AI 短剧出海」认成风电）。
    aligned = align_themes(text, chosen, limit=2, allow_match=bool(chosen))
    # "这条新闻在不在主题体系覆盖的赛道内"决定能不能按链条长度降权（见 _temper_impact）。
    # 取数异常时 evidence=None，一律不降权 —— 宁可放过，不可误伤。
    catalog, _ = _theme_catalog()
    evidence = _theme_evidence(text, catalog)
    impacts = []
    for name in aligned:
        src = next((t for t in a.themes if
                    (t.theme == name or t.theme in name)), None)
        direction = (src.direction if src and src.direction in
                     ("bullish", "bearish", "neutral") else a.direction)
        strength = src.strength if src else a.strength
        elasticity = src.elasticity if src else 1.0
        basis = (src.basis if src and src.basis else a.reasoning or "")
        # 口径：主题必须带方向 —— "没有方向的主题"和"没有主题"在展示上等价，
        # 但前者会让人以为我们判断过（规则路径早就是这么做的，LLM 路径要对齐）。
        if direction not in ("bullish", "bearish"):
            continue
        # basis 自己否定了传导链 → 这个主题不成立（见 _CHAIN_NEGATION）
        if _basis_negates_chain(basis):
            logger.debug("drop theme %s: basis negates chain (%s)", name, basis[:40])
            continue
        # 链条太长、或自己承认"弹性存疑" → 压强度甚至丢弃（见 _temper_impact）
        keep, strength, elasticity = _temper_impact(
            strength, elasticity, basis, a.reasoning or "",
            # 只要命中了**任意**主题，说明这条新闻落在体系覆盖的赛道内，LLM 给的
            # 相邻主题大概率是产业链延伸（设备→封装、HBM→存储）；一条都没命中
            # 才是纯联想。取数失败（None）按"在体系内"处理，不降权。
            in_universe=True if evidence is None else bool(evidence),
        )
        if not keep:
            logger.debug(
                "drop theme %s: weak chain over %d hops (%s)",
                name, _transmission_hops(basis), basis[:40],
            )
            continue
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
            "basis": basis[:120],
        })
    # chosen 非空但收敛失败时，align_themes 内部的 match_themes 已经做过
    # 关键词/成分股归一了，这里不必再跑一次规则（同样的逻辑）。
    # chosen 为空则**不做任何兜底** —— 那是 LLM 明确判定"没有传导链"。
    _finish_impacts(item, impacts)


def _apply_rule(item: dict) -> None:
    """规则降级：只用关键词/成分股拿主题，幅度按规则强度校准。"""
    item["assess_source"] = "rule"
    text = f"{item.get('title', '')} {item.get('content', '')}"
    _finish_impacts(item, _rule_impacts(item, text))


def recheck(items: list[dict], llm=None, *, use_cache: bool = True) -> list[dict]:
    """榜单二次评估：给"已上榜但只走了规则"的条目补一次 LLM。

    首轮 LLM 只覆盖 prefilter 头部 ``LLM_LIMIT`` 条，而配额排序是从更大的候选
    池里挑 20 条上板 —— 生产快照 973 实测 **13/20 是规则票**：没有传导链、没有
    理由、幅度按规则强度粗校准。这些恰恰是用户唯一会看到的内容。

    这里只补"没被 LLM 评估过"的那部分，一次批量调用（缓存仍先生效）；失败就
    保持规则结果不动，榜单不会因为二次评估挂掉。
    """
    if not items or llm is None:
        return items
    todo = [i for i, it in enumerate(items) if it.get("assess_source") != "llm"]
    if not todo:
        return items
    subset = [items[i] for i in todo]
    assessed: dict[int, NewsAssessment] = load_cached(subset) if use_cache else {}
    missing = [k for k in range(len(subset)) if k not in assessed]
    if missing:
        fresh = assess([subset[k] for k in missing], llm)
        mapped = {missing[local]: a for local, a in fresh.items()
                  if 0 <= local < len(missing)}
        if mapped:
            save_cached(subset, mapped, model=type(llm).__name__)
            assessed.update(mapped)
        logger.info(
            "news recheck: %d items, %d cache hit, %d llm, %d rule",
            len(subset), len(subset) - len(missing), len(mapped),
            len(missing) - len(mapped),
        )
    for k, a in assessed.items():
        _apply_assessment(subset[k], a)
        subset[k]["assess_source"] = "llm"
    return items


def rerank(items: list[dict]) -> list[dict]:
    """重算顺序分并按榜单键重排（二次评估改了 importance 之后调用）。"""
    for item in items:
        item["_order"] = (
            float(item.get("importance") or 0.0)
            * _freshness(_pub_time(item))
            * min(int(item.get("source_count", 1)), 3)
        )
    items.sort(key=_rank_key, reverse=True)
    for i, item in enumerate(items, 1):
        item["rank"] = i
    return items


def _item_key(item: dict) -> str:
    from .news_board import normalize_title

    return item.get("title_hash") or normalize_title(str(item.get("title", "")))


def prune_and_refill(
    top: list[dict],
    pool: list[dict],
    top_n: int,
    llm=None,
    *,
    use_cache: bool = True,
) -> list[dict]:
    """二次评估把条目降级成噪音后，让它让位给候选池里的下一条。

    配额排序时的 ``QUOTA_MIN_IMPORTANCE`` 门槛用的是**规则**估计的重要性 ——
    排序完成后 ``recheck`` 才用 LLM 改写 importance，而 ``rerank`` 只重排不淘汰。
    结果是"规则看着像 4 分、LLM 判成 1 分"的条目照样上榜（快照 982 末尾 5 条
    海外个股/脱口秀式新闻，importance=1.0 却排在榜上）。

    这里做三件事：
    1. 丢弃 ``assess_source == "llm"`` 且 importance 低于门槛的条目（规则票不动，
       因为规则估计本来就可能偏保守）；
    2. 从候选池按榜单键顺序补位，补位条目**先过 recheck 再判定**，避免用规则分
       估出一个高分噪音 —— 宁可多打一次 LLM，也不要让用户看到空榜；
    3. 同簇（同事件多源）仍然只占一个榜位。

    补不满就补不满：短榜比"凑数的噪音"好。
    """
    if not top or not pool:
        return top
    floor = float(QUOTA_MIN_IMPORTANCE)

    def _low(item: dict) -> bool:
        # 只有 LLM 明确判过的低分才淘汰；规则票的低分不算"定论"
        return (item.get("assess_source") == "llm"
                and float(item.get("importance") or 0.0) < floor)

    kept = [x for x in top if not _low(x)]
    dropped = len(top) - len(kept)
    if not dropped:
        return top

    seen = {_item_key(x) for x in top}
    spare = sorted(
        [
            x for x in pool
            if _item_key(x) not in seen
            and float(x.get("importance") or 0.0) >= floor
        ],
        key=_rank_key, reverse=True,
    )
    clusters = {x.get("cluster_id") for x in kept if x.get("cluster_id")}
    filled = 0
    cursor = 0
    while len(kept) < top_n and cursor < len(spare):
        need = top_n - len(kept)
        batch = spare[cursor:cursor + max(need * 2, 8)]
        cursor += len(batch)
        if llm is not None:
            recheck(batch, llm, use_cache=use_cache)
        for item in sorted(batch, key=_rank_key, reverse=True):
            if len(kept) >= top_n:
                break
            if _low(item):
                continue
            cid = item.get("cluster_id") or ""
            if cid and cid in clusters:
                continue
            if cid:
                clusters.add(cid)
            kept.append(item)
            filled += 1

    logger.info(
        "news board: recheck demoted %d item(s) below importance floor, refilled %d",
        dropped, filled,
    )
    return kept


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


# ---------------------------------------------------------------------------
# 9. 回测台账
# ---------------------------------------------------------------------------


def record_tracking(
    items: list[dict], snapshot_id: int, *, base_date: str = "",
) -> int:
    """把本轮的（主题, 方向, 区间）写入回测台账，留给事后验证。

    ``base_date`` 是预测基准日（默认今天）。同一快照 + 同一新闻 + 同一主题
    只写一次；脚本 ``scripts/news_impact_review.py`` 按 horizon 回填
    ``actual_return`` 并判定方向/区间是否命中。

    静默失败：台账缺数据只会让复盘样本变少，不该影响榜单落库。
    """
    if not items:
        return 0
    from datetime import date

    base = base_date or date.today().isoformat()
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        for imp in item.get("industry_impacts") or []:
            theme = str(imp.get("name", "") or "")
            if not theme:
                continue
            digest = title_digest(str(item.get("title", "")))
            if (digest, theme) in seen:
                continue
            seen.add((digest, theme))
            rows.append({
                "snapshot_id": int(snapshot_id or 0),
                "news_title_hash": digest,
                "title": str(item.get("title", ""))[:500],
                "theme": theme[:64],
                "direction": str(imp.get("direction") or "neutral"),
                "strength": int(imp.get("strength") or 1),
                "horizon": str(item.get("horizon") or "1d"),
                "base_date": base,
                "exp_low": float(imp.get("exp_low") or 0.0),
                "exp_high": float(imp.get("exp_high") or 0.0),
                "now": _utcnow_naive(),
            })
    if not rows:
        return 0
    try:
        from sqlalchemy import text

        from .db_ops import _get_session

        session = _get_session()
        try:
            session.execute(
                text(
                    "INSERT INTO news_impact_tracking "
                    "(snapshot_id, news_title_hash, title, theme, direction, strength, "
                    " horizon, base_date, exp_low, exp_high, created_at) "
                    "VALUES (:snapshot_id, :news_title_hash, :title, :theme, :direction, "
                    " :strength, :horizon, :base_date, :exp_low, :exp_high, :now)"
                ),
                rows,
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug("impact tracking write skipped: %s", exc)
        return 0
    logger.info("impact tracking: %d rows recorded for snapshot %s", len(rows), snapshot_id)
    return len(rows)
