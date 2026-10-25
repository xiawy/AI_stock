"""端口实现.

``DataServiceAdapter`` 是生产实现: 异常一律转成 ``Evidence(ok=False)``, 不向上抛.
理由: 数据源被封/超时是常态 (东财 push2 已被封过), 用异常表达"缺数据"会让
每一个调用点都长出一层 try, 真正该处理的异常反而被淹没.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

from .contracts import (
    BoardFlow,
    Constituent,
    Evidence,
    LimitUpRow,
    MacroVars,
    NewsImpact,
    PriceBar,
)

logger = logging.getLogger(__name__)

# 个股日资金流只有文本形态 (ai_stock/dataflows/a_stock.get_fund_flow),
# 没有结构化接口。这里做一次受控解析: 解析规则有单测, 失败即视为数据缺失,
# 绝不"猜一个数"。单位保持接口原样 (万元)。
_FUND_ROW = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}) \| main=(-?\d+)")


def parse_fund_history(text: str) -> list[float]:
    """解析资金流文本 → 逐日主力净流入 (万元). 无法解析返回空列表."""
    if not text or "Historical Daily Fund Flow" not in text:
        return []
    out: list[float] = []
    for line in text.splitlines():
        m = _FUND_ROW.match(line)
        if m:
            out.append(float(m.group(2)))
    return out


class DataServiceAdapter:
    """生产取数实现. 所有方法失败时返回空, 不抛."""

    def __init__(self, ds=None, trade_date: str = ""):
        self._ds = ds
        self.trade_date = trade_date

    @property
    def ds(self):
        if self._ds is None:
            from ..data_service import get_data_service

            self._ds = get_data_service()
        return self._ds

    # ---- 板块 --------------------------------------------------------------

    def board_flow(self) -> tuple[list[BoardFlow], Evidence]:
        try:
            rows = self.ds.get_all_board_fund_flow() or []
        except Exception as exc:
            return [], _missing("board_flow", "eastmoney_push2", exc)

        boards = [
            BoardFlow(
                code=str(r.get("code", "")),
                name=str(r.get("name", "")),
                change_pct=_f(r.get("change_pct")),
                main_net_inflow=_f(r.get("main_net_inflow")),
                amount=_f(r.get("amount")),
            )
            for r in rows
            if r.get("code") and r.get("name")
        ]
        if not boards:
            return [], Evidence(
                kind="board_flow", source="eastmoney_push2", ok=False,
                empty_reason="全部板块数据源返回空",
            )
        return boards, Evidence(
            kind="board_flow", source="eastmoney_push2", payload={"n": len(boards)},
        )

    def constituents(self, board_code: str, top_n: int = 20) -> list[Constituent]:
        try:
            rows = self.ds.get_industry_stocks(board_code, top_n=top_n) or []
        except Exception as exc:
            logger.warning("constituents(%s) failed: %s", board_code, exc)
            return []
        return [
            Constituent(
                code=str(r.get("code", "")),
                name=str(r.get("name", "")),
                change_pct=_f(r.get("change_pct")),
                amount=_f(r.get("amount")),
                main_net_inflow=_f(r.get("main_net_inflow")),
            )
            for r in rows
            if r.get("code")
        ]

    # ---- 涨停 / 概念 -------------------------------------------------------

    def limit_up(self, days: int = 5) -> tuple[list[LimitUpRow], Evidence]:
        try:
            rows = self.ds.get_limit_up_stocks(days=days) or []
        except Exception as exc:
            return [], _missing("limit_up", "ths_getharden", exc)

        items = [
            LimitUpRow(
                code=str(r.get("code", "")),
                name=str(r.get("name", "")),
                trade_date=str(r.get("date", "")),
                reason_tags=list(r.get("reason_tags") or []),
            )
            for r in rows
            if r.get("code")
        ]
        if not items:
            return [], Evidence(
                kind="limit_up", source="ths_getharden", ok=False,
                empty_reason="涨停池为空或当日无涨停",
            )
        return items, Evidence(
            kind="limit_up", source="ths_getharden", payload={"n": len(items)},
        )

    def concepts(self, symbol: str) -> list[str]:
        try:
            return list(self.ds.get_concept_blocks(symbol) or [])
        except Exception as exc:
            logger.debug("concepts(%s) failed: %s", symbol, exc)
            return []

    # ---- 资金 --------------------------------------------------------------

    def fund_flow(self, symbols: Sequence[str], days: int = 20) -> dict[str, list[float]]:
        """逐股主力净流入序列 (万元). 取不到的股不出现在返回里."""
        from ai_stock.dataflows.a_stock import get_fund_flow

        out: dict[str, list[float]] = {}
        curr = self.trade_date or _market_today()
        for symbol in symbols:
            try:
                text = get_fund_flow(symbol, curr, include_history=True)
            except Exception as exc:
                logger.debug("fund_flow(%s) failed: %s", symbol, exc)
                continue
            series = parse_fund_history(text)
            if series:
                out[symbol] = series[-days:]
        return out

    # ---- 宏观 --------------------------------------------------------------

    def macro(self) -> tuple[MacroVars, Evidence]:
        from ai_stock.dataflows.macro import fetch_macro

        raw = fetch_macro()
        vars_ = MacroVars(**{k: v for k, v in raw.items() if v is not None})
        missing = vars_.missing()
        if len(missing) >= 2:
            return vars_, Evidence(
                kind="macro", source="yfinance", ok=False,
                empty_reason=f"缺失 {len(missing)} 个变量: {','.join(missing)}",
            )
        return vars_, Evidence(kind="macro", source="yfinance", payload=raw)

    # ---- 新闻 --------------------------------------------------------------

    def news_impacts(self, days: int = 3) -> tuple[list[NewsImpact], Evidence]:
        """新闻榜的主题级方向 (只读, 跨库到 backend/data/aistock.db).

        为什么读新闻榜而不是自己判方向:
        - 新闻榜已经做过主题归一、方向收敛与强度压制 (弱传导链会被丢弃);
        - 自己再判一次必然与榜单口径不一致, 前端看到的方向会和报告打架;
        - 跨库是"只读一次 SQL", 不写库、不带事务, 不会把两套数据生命周期绑死.
        """
        try:
            rows = _query_news_impacts(days)
        except Exception as exc:
            return [], _missing("news_impacts", "news_board", exc)

        # 同主题同日只保留最强的一条: 三条同主题新闻是同一份事实的复述,
        # 累加会让它在矩阵里凭空变成"强证据".
        best: dict[tuple[str, str], NewsImpact] = {}
        for raw in rows:
            key = (raw.theme, raw.base_date)
            prev = best.get(key)
            if prev is None or raw.strength > prev.strength:
                best[key] = raw
        items = list(best.values())
        if not items:
            return [], Evidence(
                kind="news_impacts", source="news_board", ok=False,
                empty_reason=f"近 {days} 日新闻榜无主题级方向",
            )
        return items, Evidence(
            kind="news_impacts", source="news_board", payload={"n": len(items)},
        )

    # ---- 龙虎榜席位 --------------------------------------------------------

    def institutional(self, symbols: Sequence[str]) -> tuple[dict[str, bool], Evidence]:
        """机构专用席位净买入 → True. 未上榜/取不到 → 不出键 (=无法判定).

        只在上榜股里查席位: ≥3 连板每天不足 10 只, 但全市场逐股查会把
        东财 datacenter 打到限流, 而限流的代价是整块数据一起缺.
        """
        from ai_stock.dataflows.a_stock import _eastmoney_datacenter

        wanted = [s for s in symbols if s]
        if not wanted:
            return {}, Evidence(kind="institutional", source="eastmoney_lhb",
                                ok=False, empty_reason="无高标标的")
        end = self.trade_date or _market_today()
        start = _days_before(end, 7)
        try:
            listed = _lhb_listed(_eastmoney_datacenter, start, end)
        except Exception as exc:
            return {}, _missing("institutional", "eastmoney_lhb", exc)

        out: dict[str, bool] = {}
        for code in wanted:
            if code not in listed:
                continue            # 未上榜 = 没有席位事实, 不是"游资接力"
            try:
                net = _lhb_institution_net(_eastmoney_datacenter, code, start, end)
            except Exception as exc:
                logger.debug("lhb seats(%s) failed: %s", code, exc)
                continue
            if net is None:
                continue
            # 机构净卖出时不给 False: False 在 rules.leader.judge_fund 里意味着
            # "游资接力", 而机构出货是完全不同的结论. 让它回落到 D0/D1 判定.
            if net > 0:
                out[code] = True
        if not out:
            return {}, Evidence(
                kind="institutional", source="eastmoney_lhb", ok=False,
                empty_reason="高标无机构专用席位净买入记录",
            )
        return out, Evidence(
            kind="institutional", source="eastmoney_lhb", payload={"n": len(out)},
        )

    # ---- 判定用日线 --------------------------------------------------------

    def bars(self, symbol: str, lookback_days: int = 10) -> list[PriceBar]:
        """日线 (含涨跌幅). 停牌/无数据返回空列表 → 判定层落 unjudged."""
        try:
            df = self.ds.get_ohlcv(symbol, lookback_days=lookback_days, live=False)
        except Exception as exc:
            logger.debug("bars(%s) failed: %s", symbol, exc)
            return []
        if df is None or len(df) == 0:
            return []

        out: list[PriceBar] = []
        closes = list(df["Close"])
        for idx, row in enumerate(df.itertuples(index=False)):
            date_str = str(getattr(row, "Date", ""))[:10]
            prev = closes[idx - 1] if idx > 0 else None
            pct = ((closes[idx] - prev) / prev * 100.0) if prev else 0.0
            out.append(PriceBar(
                bar_date=date_str,
                close=float(closes[idx]),
                pct_change=round(pct, 2),
                provisional=False,      # live=False: 只取已完成日线
            ))
        return out


class ReplayAdapter:
    """回放实现: 从已落库的证据快照读, 零网络.

    用途: 用最近 20 个交易日的真实快照回归测试规则改动 —— 改阈值之前先跑一遍,
    看四个反例是否仍然正确地被降级.
    """

    def __init__(self, snapshot: dict):
        self._s = snapshot

    def board_flow(self) -> tuple[list[BoardFlow], Evidence]:
        rows = [BoardFlow(**b) for b in self._s.get("board_flow", [])]
        return rows, Evidence(kind="board_flow", source="replay", ok=bool(rows))

    def constituents(self, board_code: str, top_n: int = 20) -> list[Constituent]:
        rows = self._s.get("constituents", {}).get(board_code, [])
        return [Constituent(**c) for c in rows][:top_n]

    def limit_up(self, days: int = 5) -> tuple[list[LimitUpRow], Evidence]:
        rows = [LimitUpRow(**r) for r in self._s.get("limit_up", [])]
        return rows, Evidence(
            kind="limit_up", source="replay", ok=bool(rows),
            payload={"n": len(rows)},
        )

    def concepts(self, symbol: str) -> list[str]:
        return list(self._s.get("concepts", {}).get(symbol, []))

    def fund_flow(self, symbols: Sequence[str], days: int = 20) -> dict[str, list[float]]:
        data = self._s.get("fund_flow", {})
        return {s: list(data[s])[-days:] for s in symbols if s in data}

    def macro(self) -> tuple[MacroVars, Evidence]:
        raw = self._s.get("macro", {})
        return MacroVars(**raw), Evidence(kind="macro", source="replay", ok=bool(raw))

    def news_impacts(self, days: int = 3) -> tuple[list[NewsImpact], Evidence]:
        rows = [NewsImpact(**n) for n in self._s.get("news_impacts", [])]
        return rows, Evidence(
            kind="news_impacts", source="replay", ok=bool(rows),
            empty_reason="" if rows else "回放快照无新闻证据",
            payload={"n": len(rows)},
        )

    def institutional(self, symbols: Sequence[str]) -> tuple[dict[str, bool], Evidence]:
        raw = self._s.get("institutional", {}) or {}
        out = {k: bool(v) for k, v in raw.items() if k in set(symbols)}
        return out, Evidence(
            kind="institutional", source="replay", ok=bool(out),
            empty_reason="" if out else "回放快照无席位数据",
            payload={"n": len(out)},
        )

    def bars(self, symbol: str, lookback_days: int = 10) -> list[PriceBar]:
        rows = self._s.get("bars", {}).get(symbol, [])
        return [PriceBar(**b) for b in rows][-lookback_days:]


# ---- 工具 ----------------------------------------------------------------


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _missing(kind: str, source: str, exc: Exception) -> Evidence:
    return Evidence(
        kind=kind, source=source, ok=False,
        empty_reason=f"{type(exc).__name__}: {str(exc)[:100]}",
    )


def _market_today() -> str:
    from ..calendar_utils import get_trade_date

    try:
        return str(get_trade_date())
    except Exception:
        return ""


def _days_before(day: str, n: int) -> str:
    try:
        return (date.fromisoformat(day[:10]) - timedelta(days=n)).isoformat()
    except ValueError:
        return day


# ---- 新闻榜: 只读一次 SQL --------------------------------------------------


def _query_news_impacts(days: int) -> list[NewsImpact]:
    """读最近 ``days`` 天已完成快照里的主题级影响.

    表结构见 ai_stock/pipeline/db_models.py: ``news_items.industry_impacts_json``
    是 [{name, direction, strength, horizon, exp_low, exp_high, basis}].
    direction=neutral 的条目已经被上游丢弃, 这里再过滤一次是为了兼容旧快照.
    """
    import json

    from sqlalchemy import text

    from ai_stock.pipeline.db_ops import _get_session

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    session = _get_session()
    if session is None:
        raise RuntimeError("新闻榜库不可用")

    try:
        rows = session.execute(
            text(
                "SELECT n.industry_impacts_json AS impacts, n.horizon AS horizon, "
                "       n.source_count AS source_count, s.snapshot_time AS snap_time "
                "FROM news_items n JOIN impact_snapshots s ON s.id = n.snapshot_id "
                "WHERE s.status = 'completed' AND s.snapshot_time >= :since "
                "ORDER BY s.snapshot_time DESC, n.importance DESC LIMIT 300"
            ),
            {"since": since},
        ).mappings().all()
    finally:
        session.close()

    out: list[NewsImpact] = []
    for row in rows:
        base = str(row["snap_time"])[:10]
        try:
            impacts = json.loads(row["impacts"] or "[]")
        except ValueError:
            continue
        for imp in impacts:
            theme = str(imp.get("name") or "").strip()
            direction = str(imp.get("direction") or "neutral")
            if not theme or direction == "neutral":
                continue
            out.append(NewsImpact(
                theme=theme,
                direction=direction,
                strength=int(imp.get("strength") or 1),
                horizon=str(imp.get("horizon") or row["horizon"] or ""),
                source_count=int(row["source_count"] or 1),
                base_date=base,
                exp_low=_f(imp.get("exp_low")),
                exp_high=_f(imp.get("exp_high")),
            ))
    return out


# ---- 龙虎榜: 东财 datacenter 结构化查询 ------------------------------------

_LHB_LIST = "RPT_DAILYBILLBOARD_DETAILSNEW"
_LHB_BUY = "RPT_BILLBOARD_DAILYDETAILSBUY"
_LHB_SELL = "RPT_BILLBOARD_DAILYDETAILSSELL"

# 机构专用席位的营业部代码 (东财约定): "0" = 机构专用
_INSTITUTION_SEAT = "0"


def _lhb_listed(query, start: str, end: str) -> set[str]:
    rows = query(
        _LHB_LIST,
        filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')",
        page_size=500, sort_columns="TRADE_DATE", sort_types="-1",
    )
    return {str(r.get("SECURITY_CODE") or "") for r in rows or []}


def _lhb_institution_net(query, code: str, start: str, end: str) -> Optional[float]:
    """机构专用席位净额 (元). 无任何席位数据返回 None (= 无法判定)."""
    filters = (
        f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')(SECURITY_CODE=\"{code}\")"
    )
    net = 0.0
    seen = False
    for report, side in ((_LHB_BUY, "BUY"), (_LHB_SELL, "SELL")):
        rows = query(report, filter_str=filters, page_size=50,
                     sort_columns=side, sort_types="-1")
        for row in rows or []:
            seen = True
            if str(row.get("OPERATEDEPT_CODE")) != _INSTITUTION_SEAT:
                continue
            buy = _f(row.get("BUY"))
            sell = _f(row.get("SELL"))
            net += buy - sell
    return net if seen else None
