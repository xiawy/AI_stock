"""端口层 —— 取数抽象.

两个实现:
- ``DataServiceAdapter``  生产: 调 DataService, 异常→空列表 + Evidence(ok=False)
- ``ReplayAdapter``       回放: 读已落库的证据快照, 零网络

这层存在的唯一理由: 让"回放最近 20 个交易日 + 四个反例自动降级"这条验收
标准可执行。没有它, 验收只能靠人肉跑生产.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from .contracts import (
    BoardFlow,
    Constituent,
    Evidence,
    LimitUpRow,
    MacroVars,
    NewsImpact,
    PriceBar,
)


class EvidencePort(Protocol):
    """取数端口. 实现者必须保证: 失败返回空列表 + Evidence(ok=False), 不抛异常."""

    def board_flow(self) -> tuple[list[BoardFlow], Evidence]: ...

    def constituents(self, board_code: str, top_n: int = 20) -> list[Constituent]: ...

    def limit_up(self, days: int = 5) -> tuple[list[LimitUpRow], Evidence]: ...

    def concepts(self, symbol: str) -> list[str]: ...

    def fund_flow(self, symbols: Sequence[str], days: int = 20) -> dict[str, list[float]]: ...

    def macro(self) -> tuple[MacroVars, Evidence]: ...

    def news_impacts(self, days: int = 3) -> tuple[list[NewsImpact], Evidence]:
        """新闻榜的**主题级**方向证据. 只读已有评估结果, 不在这里重新判方向."""
        ...

    def institutional(self, symbols: Sequence[str]) -> tuple[dict[str, bool], Evidence]:
        """龙虎榜机构专用席位净买入判定. 缺键 = 无法判定 (不猜)."""
        ...

    def bars(self, symbol: str, lookback_days: int = 10) -> list[PriceBar]: ...


class RepoPort(Protocol):
    """持久化端口. 由 repo.StrategyRepo 实现."""

    def save_evidence(self, trade_date: str, rows: Sequence[Evidence]) -> None: ...

    def load_report(self, trade_date: str, snapshot_type: str) -> dict | None: ...
