"""三榜的展示口径 (纯函数, 不碰数据库/网络)。

这里只做两类事:
1. **口径统一** —— 把"该按什么排序、该标什么状态"写在一个地方。此前热股榜
   按 ``confidence``(入池静态分) 排序, 而买入链路按 ``dyn_confidence`` 排队,
   榜单第 1 名可能正是系统这轮准备踢掉的票。
2. **跨期对比** —— 把"今天变了什么"算出来。三个榜的原始数据形状不同, 但
   "排名 → 新进/上升/下降/掉榜"的算法是同一套, 所以只写一份。

刻意不 import 任何 DB 会话: 这一层可以被单测直接覆盖, 也可以在回放历史时
脱离数据库使用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

# 与 quant.config 保持一致。这里不 import, 是因为 backend 进程不一定加载
# quant 的完整依赖树; 两个数量级必须对齐, 改一处要同时改另一处。
CONF_DEFAULT = 0.5
CONF_KICK_THRESHOLD = 0.25
# 距踢出多远算"危险": 1.5 倍门槛 (0.375)。取 1.5 而不是 1.0, 是为了在
# 真正被踢之前给出可视预警窗口 —— 榜单的价值在于"提前看见"。
NEAR_KICK_RATIO = 1.5
# 动态分偏离基准多少算"增强/衰减"。0.05 小于一次衰减步长(CONF_DECAY_FACTOR
# 约 0.9), 又大于浮点噪声, 只用来做定性标签, 不参与任何计算。
DYN_STATE_EPS = 0.05

CHANGE_NEW = "new"
CHANGE_UP = "up"
CHANGE_DOWN = "down"
CHANGE_SAME = "same"
CHANGE_OUT = "out"


# ---------------------------------------------------------------------------
# 热股榜: 排序口径 + 状态标注
# ---------------------------------------------------------------------------


def pool_sort_key(stock: dict) -> tuple[float, float]:
    """热股榜排序键 = (动态置信度, 入池分)。

    动态置信度优先, 与 ``buy.py`` 的候选排队口径一致; 入池分只作同分时的
    次序, 避免"同分随机排"让人以为名次有意义。
    """
    dyn = _num(stock.get("dyn_confidence"), CONF_DEFAULT)
    conf = _num(stock.get("confidence"), 0.0)
    return (dyn, conf)


def sort_pool(stocks: Sequence[dict]) -> list[dict]:
    return sorted(stocks, key=pool_sort_key, reverse=True)


def annotate_pool(stocks: Sequence[dict]) -> list[dict]:
    """给热股榜行补上"这条还活着吗"的展示字段。

    只加字段不改原值: ``confidence`` 仍是入池静态分(0-10), ``dyn_confidence``
    仍是动态分(0-1)。两个分并列展示, 才能让"入池看好、现在衰减"看得出来。
    """
    out: list[dict] = []
    for idx, raw in enumerate(sort_pool(stocks), 1):
        row = dict(raw)
        dyn = _num(row.get("dyn_confidence"), CONF_DEFAULT)
        row["rank"] = idx
        row["dyn_state"] = _dyn_state(dyn)
        row["is_tail"] = (row.get("stage_batch") or "BODY") == "TAIL"
        row["near_kick"] = dyn < CONF_KICK_THRESHOLD * NEAR_KICK_RATIO
        row["expire_in_days"] = _days_until(row.get("observe_expire"))
        out.append(row)
    return out


def _dyn_state(dyn: float) -> str:
    if dyn > CONF_DEFAULT + DYN_STATE_EPS:
        return "增强"
    if dyn < CONF_DEFAULT - DYN_STATE_EPS:
        return "衰减"
    return "正常"


def _days_until(iso: Optional[str]) -> Optional[int]:
    """距观察期到期的自然日数。解析失败返回 None(而不是 0): 0 会被读成"今天到期"。"""
    if not iso:
        return None
    try:
        expire = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    return (expire - datetime.now(timezone.utc)).days


# ---------------------------------------------------------------------------
# 通用跨期对比
# ---------------------------------------------------------------------------


def diff_ranked(
    current: Sequence[dict],
    previous: Sequence[dict],
    key: str,
) -> dict:
    """对比两期有序列表, 返回 ``{rows, dropped, summary}``。

    *current* 必须已按展示顺序排好(第 0 个是第 1 名); *previous* 同理。
    ``key`` 是识别同一实体的字段(行业名 / 簇 id / 股票代码)。

    只做"位置"的对比, 不解释原因 —— 解释必须来自已结构化的字段, 让 LLM
    现编"为什么涨"会让同一个历史视图每天说法不同。
    """
    prev_rank = {
        str(r.get(key) or ""): i
        for i, r in enumerate(previous, 1)
        if r.get(key)
    }
    cur_keys: set[str] = set()
    rows: list[dict] = []
    for i, raw in enumerate(current, 1):
        row = dict(raw)
        ident = str(row.get(key) or "")
        cur_keys.add(ident)
        before = prev_rank.get(ident)
        if before is None:
            change, delta = CHANGE_NEW, None
        elif before > i:
            change, delta = CHANGE_UP, before - i
        elif before < i:
            change, delta = CHANGE_DOWN, before - i
        else:
            change, delta = CHANGE_SAME, 0
        row["prev_rank"] = before
        row["delta_rank"] = delta
        row["change"] = change
        rows.append(row)

    dropped = [
        {**dict(r), "change": CHANGE_OUT, "prev_rank": prev_rank.get(str(r.get(key) or "")),
         "delta_rank": None}
        for r in previous
        if str(r.get(key) or "") and str(r.get(key)) not in cur_keys
    ]
    return {
        "rows": rows,
        "dropped": dropped,
        "summary": _diff_summary(rows, dropped, key),
    }


def _diff_summary(rows: Iterable[dict], dropped: Sequence[dict], key: str) -> dict:
    rows = list(rows)

    def _label(row: dict) -> str:
        """三个榜的"名字"字段不同(行业名 / 标题 / 股票名)。

        漏掉这一层，行业榜的变化摘要会全是 null —— 界面看起来"没有变化"，
        实际是取错了字段。
        """
        for field in ("name", "title", "industry", "symbol", key):
            value = row.get(field)
            if value:
                return str(value)
        return ""

    return {
        "new": [_label(r) for r in rows if r.get("change") == CHANGE_NEW],
        "up": [
            {"label": _label(r), "delta": r.get("delta_rank")}
            for r in rows if r.get("change") == CHANGE_UP
        ],
        "down": [
            {"label": _label(r), "delta": r.get("delta_rank")}
            for r in rows if r.get("change") == CHANGE_DOWN
        ],
        "out": [_label(r) for r in dropped],
        "has_previous": True,
    }


EMPTY_DIFF = {
    "rows": [],
    "dropped": [],
    "summary": {"new": [], "up": [], "down": [], "out": [], "has_previous": False},
}


def no_previous_diff(rows: Sequence[dict]) -> dict:
    """没有上一期可比时的返回: 不假装"全是新进"。

    "没有上一期"和"上一期为空"是两件事, 混在一起会让用户以为榜单今天
    大换血。所以 ``has_previous=False``, 前端据此隐藏变化列。
    """
    return {
        "rows": [dict(r) for r in rows],
        "dropped": [],
        "summary": {"new": [], "up": [], "down": [], "out": [], "has_previous": False},
    }


# ---------------------------------------------------------------------------
# 行业榜: 连榜天数
# ---------------------------------------------------------------------------


def consecutive_days(history: Sequence[dict], industry: str, key: str = "industry") -> int:
    """该行业在最近连续几期榜上出现(*history* 新→旧)。"""
    days = 0
    for snap in history:
        names = {str(r.get(key) or "") for r in snap.get("rows") or []}
        if industry in names:
            days += 1
        else:
            break
    return days


def _num(value, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
