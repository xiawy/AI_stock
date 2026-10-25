"""交叉验证矩阵 (M5, PRD §5).

纯规则, 不用 LLM: 同向判定是符号运算, 交给模型只会让同一输入得出不同结论,
而它恰恰是"共振 / 背离 / 伪共振"的唯一证据.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts import BasketVerdict, CrossMatrix, MatrixRow

EPS = 0.5   # 涨跌幅/资金/情绪的"零"带: ±0.5% 内视为方向不明


def sign(value: float | None, eps: float = EPS) -> int:
    if value is None:
        return 0
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def build_matrix(
    verdicts: Sequence[BasketVerdict],
    *,
    flow_sign: Mapping[str, int],
    news_sign: Mapping[str, int],
    sources: Mapping[str, set[str]] | None = None,
) -> CrossMatrix:
    """按篮子(而非板块名)对齐三方视角.

    伪共振: 三方关键事实引自同一来源时, 媒体叙事一旦失真, 三人会同时错.
    这类行不得判 ✅, 置信度上限降为「中」.
    """
    sources = sources or {}
    rows: list[MatrixRow] = []

    for v in verdicts:
        name = v.basket.name
        s_sign = sign(v.synth_change_pct)
        f_sign = flow_sign.get(name, 0)
        n_sign = news_sign.get(name, 0)

        consistency = _consistency(s_sign, f_sign, n_sign)
        src = sources.get(name, set())
        pseudo = consistency.startswith("✅") and len(src) < 2
        note = "同源事实, 不得判共振" if pseudo else ""

        rows.append(MatrixRow(
            basket_name=name,
            sector_view=f"{v.synth_change_pct:+.2f}% ({v.rating_cap})",
            flow_view=_flow_text(f_sign),
            news_view=_news_text(n_sign),
            sector_sign=s_sign,
            flow_sign=f_sign,
            news_sign=n_sign,
            consistency=consistency,
            pseudo_resonance=pseudo,
            note=note,
        ))

    return CrossMatrix(
        rows=rows,
        resonance_n=sum(1 for r in rows if r.consistency.startswith("✅")),
        divergence_n=sum(1 for r in rows if r.consistency.startswith("❌")),
    )


def _consistency(s: int, f: int, n: int) -> str:
    signals = [x for x in (s, f, n) if x != 0]
    if len(signals) <= 1:
        return "证据不足"
    if len(signals) == 3:
        if len(set(signals)) == 1:
            return "✅ 三方共振"
        return "❌ 三方冲突"
    # 两方有方向
    return "⚠️ 两方一致" if signals[0] == signals[1] else "❌ 两方冲突"


def _flow_text(s: int) -> str:
    return {1: "净流入", -1: "净流出", 0: "无明显方向"}[s]


def _news_text(s: int) -> str:
    """新闻面用多空措辞, 不用"流入/流出" —— 三方文本混用会让读者把新闻当资金."""
    return {1: "利多", -1: "利空", 0: "无方向"}[s]
