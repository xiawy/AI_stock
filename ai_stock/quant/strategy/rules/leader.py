"""高标梯队与驱动溯源 (M2, PRD §5).

``classify`` 是全系统唯一判定"能否外推为板块逻辑"的地方.
分散到多处判定必然分叉 —— 这是纪律, 不是风格偏好.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from ..contracts import Attribution, FundVerdict, LeaderTrace, LimitUpRow
from .basket import driver_of

# 进梯队的最小连板数 (≥3 连板才算"高标", PRD 要求凡出现即溯源)
MIN_LEADER_BOARDS = 3


def count_boards(rows: Sequence[LimitUpRow]) -> dict[str, int]:
    """按交易日去重计数连板数. 同一天多次出现的同一代码只算一次."""
    dates: dict[str, set[str]] = {}
    for row in rows:
        dates.setdefault(row.code, set()).add(row.trade_date)
    return {code: len(d) for code, d in dates.items()}


def board_type(boards: int) -> str:
    if boards <= 1:
        return "首板"
    if boards >= 4:
        return f"{boards}连板"
    return "二连板" if boards == 2 else "三连板"


def classify(real_driver: str, has_public_catalyst: bool) -> tuple[Attribution, bool]:
    """返回 (归属类型, 是否可外推为板块逻辑).

    并购重组/借壳/易主 → 独立事件, 一律不可外推 (PRD 硬约束).
    """
    if real_driver == "并购重组":
        return "独立事件", False
    if not has_public_catalyst:
        return "无公开催化", False
    return "子概念龙头", True


def judge_fund(
    d0: Optional[float],
    d1: Optional[float],
    institutional_seats: Optional[bool],
) -> FundVerdict:
    """资金验证.

    "价格连板但 D0 净流出" = 出货型连板, 必须单独成立 —— 它比任何席位信息
    都更早、更可靠. 席位信息取不到时不做机构/游资的臆测.
    """
    if d0 is None:
        return "无法判定"
    if d0 < 0:
        return "边拉边撤"
    if institutional_seats is True:
        return "机构承接"
    if institutional_seats is False:
        return "游资接力"
    if d1 is not None and d0 > d1:
        return "游资接力"
    return "无法判定"


def build_traces(
    rows: Sequence[LimitUpRow],
    *,
    official_board: Mapping[str, str] | None = None,
    fund: Mapping[str, Sequence[float]] | None = None,
    institutional: Mapping[str, Optional[bool]] | None = None,
    min_boards: int = MIN_LEADER_BOARDS,
) -> list[LeaderTrace]:
    """产出高标溯源. 梯队为空时返回空列表 —— 不为了凑数降低门槛."""
    official_board = official_board or {}
    fund = fund or {}
    institutional = institutional or {}

    counts = count_boards(rows)
    tags_by_code: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for row in rows:
        tags_by_code.setdefault(row.code, []).extend(row.reason_tags)
        names.setdefault(row.code, row.name)

    traces: list[LeaderTrace] = []
    for code, boards in counts.items():
        if boards < min_boards:
            continue
        tags = tags_by_code.get(code, [])
        real_driver = driver_of(tags)
        # 有归因标签即视为存在公开催化; 无标签 → 无公开催化 (不可外推)
        attribution, extrapolable = classify(real_driver, bool(tags))
        series = list(fund.get(code, ()))
        d0 = series[-1] if series else None
        d1 = series[-2] if len(series) >= 2 else None
        traces.append(LeaderTrace(
            symbol=code,
            name=names.get(code, ""),
            boards=boards,
            board_type=board_type(boards),
            official_board=official_board.get(code, ""),
            real_driver=real_driver,
            attribution=attribution,
            extrapolable=extrapolable,
            fund_verdict=judge_fund(d0, d1, institutional.get(code)),
            exclude_from_basket=not extrapolable,
            reason_tags=tags,
        ))

    traces.sort(key=lambda t: t.boards, reverse=True)
    return traces


def ladder_health(traces: Sequence[LeaderTrace]) -> str:
    """梯队健康度: 完整 / 断层 / 仅孤标 / 无高标.

    孤标 + 断层 = 情绪脆弱, 即便指数上涨也要在主线上打折扣 (PRD §3.2).
    """
    if not traces:
        return "无高标"
    if len(traces) == 1:
        return "仅孤标"
    top = traces[0].boards
    if len(traces) >= 3 and top - traces[2].boards <= 1:
        return "完整"
    return "断层"
