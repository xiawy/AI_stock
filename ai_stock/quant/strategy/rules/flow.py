"""影子板块资金序列与风格定性 (M3, PRD §5).

影子板块 = 没有官方指数、但有明确成分的结构行情. 用个股资金加总造序列,
让"风电出海"这类结构概念也能算三日资金动能.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts import BoardFlow, ShadowSeries


def aggregate(
    name: str,
    members: Sequence[str],
    flows: Mapping[str, Sequence[float]],
) -> ShadowSeries:
    """三日资金加总. 任一日缺成员数据即整体缺失 —— 不用部分样本冒充全样本."""
    series: list[float] = []
    for idx in (-3, -2, -1):
        values = [
            flows[s][idx] for s in members
            if s in flows and len(flows[s]) >= 3
        ]
        series.append(sum(values) if len(values) == len(members) else 0.0)

    covered = sum(1 for s in members if s in flows and len(flows[s]) >= 3)
    # 严格全员: 用部分成员加总冒充全样本, 会让"影子板块"比板块本身更极端.
    # 宁可标"数据缺失", 也不给一个口径不干净的序列.
    if covered < len(members):
        return ShadowSeries(basket_name=name, members=list(members), confidence="低",
                            pattern="数据缺失")

    d2, d1, d0 = series
    return ShadowSeries(
        basket_name=name,
        members=list(members),
        d2=round(d2, 2),
        d1=round(d1, 2),
        d0=round(d0, 2),
        pattern=pattern(d2, d1, d0),
        confidence="高" if covered == len(members) else "中",
    )


def pattern(d2: float, d1: float, d0: float) -> str:
    """六态形态. 先判单调性再判符号, 否则"加速流入"会被"持续流入"吃掉."""
    if d0 > 0 and d1 > 0 and d0 >= d1 >= d2:
        return "加速流入"
    if d0 > 0 and d1 > 0:
        return "流入放缓" if d0 < d1 else "持续流入"
    if d0 < 0 and d1 < 0 and d0 <= d1 <= d2:
        return "加速流出"
    if d0 < 0 and d1 < 0:
        return "流出放缓" if d0 > d1 else "持续流出"
    if d0 > 0 >= d1:
        return "转流入"
    if d0 < 0 <= d1:
        return "转流出"
    return "方向反复"


def flow_sign(shadow: ShadowSeries) -> int:
    """给交叉验证矩阵用的方向符号: 看 D0 绝对额与 D0 相对 D1 的变化."""
    if shadow.d0 is None:
        return 0
    if shadow.d0 > 0:
        return 1
    if shadow.d0 < 0:
        return -1
    return 0


def style(defensive: float, growth: float, middle: float) -> tuple[str, bool]:
    """大盘风格定性. 哑铃态必须单独成立: 防御端与成长端同时吸金、中间失血,
    简单归入"防御"会把真正进攻的那一半藏起来.
    """
    if defensive > 0 and growth > 0 and middle < 0:
        return "哑铃态(红利打底 + 成长进攻)", True
    peak = max(("防御", defensive), ("进攻", growth), ("轮动", middle), key=lambda kv: kv[1])
    if peak[1] <= 0:
        return "资金整体流出(无有效风格)", False
    return peak[0], False


# ---------------------------------------------------------------------------
# 防御端 / 成长端板块口径
# ---------------------------------------------------------------------------
# 只放**板块名里能稳定出现的专指片段**, 不放 "央企"/"中字头"/"价值" 这类
# 泛词 —— 它们会把一半板块同时算进防御端, 口径立刻失效.
# 匹配顺序: 先成长后防御. 「电力设备」属于成长, 「电力」属于防御, 而前者
# 包含后者, 顺序反了就会把整个新能源设备链记成红利资产.
_GROWTH_PARTS = (
    "半导体", "芯片", "集成电路", "电子", "消费电子", "元件", "光学", "面板",
    "计算机", "软件", "信创", "通信", "光模块", "服务器", "算力", "游戏", "传媒",
    "电池", "锂", "光伏", "风电", "储能", "电力设备", "新能源", "汽车零部件",
    "机器人", "机械", "军工", "航天", "医药", "生物", "医疗器械", "创新药",
    "新材料", "化工", "有色", "稀土",
)
_DEFENSIVE_PARTS = (
    "银行", "保险", "公用事业", "电力", "燃气", "水务", "供水", "环保",
    "煤炭", "石油", "石化", "高速公路", "港口", "铁路", "机场", "航运",
    "红利", "高股息", "白酒", "食品", "饮料", "农业", "养殖", "建筑", "房地产",
)

STYLE_WARMUP = 4   # 任一端匹配到的板块少于 4 个 → 口径不成立, 不判风格


def classify_board(name: str) -> str:
    """板块 → growth / defensive / other. 成长优先, 保证电力设备不被电力抢走."""
    text = name or ""
    if any(p in text for p in _GROWTH_PARTS):
        return "growth"
    if any(p in text for p in _DEFENSIVE_PARTS):
        return "defensive"
    return "other"


def style_from_boards(boards: Sequence["BoardFlow"]) -> tuple[str, bool]:
    """用板块资金流的三端均值判风格. 数据不足返回 ("", False).

    用**均值**而非合计: 成长端板块数量天然多于防御端, 合计值必然偏正,
    每天都会得出"进攻"这个假结论 —— 那正是把风格定性留空的原始原因.
    """
    buckets: dict[str, list[float]] = {"growth": [], "defensive": [], "other": []}
    for b in boards:
        buckets[classify_board(b.name)].append(float(b.main_net_inflow or 0.0))
    if min(len(buckets["growth"]), len(buckets["defensive"])) < STYLE_WARMUP:
        return "", False
    if not buckets["other"]:
        return "", False

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values)

    return style(
        defensive=_avg(buckets["defensive"]),
        growth=_avg(buckets["growth"]),
        middle=_avg(buckets["other"]),
    )
