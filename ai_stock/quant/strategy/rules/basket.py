"""篮子构建与有效性裁决 (M1, PRD §5).

两个纯函数:
- ``build_drafts``  把"某板块涨了"拆成"哪几个子概念在涨" (需要外部取好的成分股/概念/归因)
- ``synthesize``    合成涨跌幅 + 证据分布 + 评级上限

为什么必须纯: 这是团队连续复发三次的系统性错误
(某锂矿子概念 2 只、某航运子概念 3 只、某 CPO 子组件 5 只里只有 1 只有证据),
必须有自动化测试守着, 不能靠人记住.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts import (
    BasketDraft,
    BasketVerdict,
    BoardFlow,
    Constituent,
    Driver,
    Grade,
    RatingCap,
    Scope,
)

# 有证据样本门槛 (PRD §5 M1). 改这里必须同步改 PRD.
STRONG_VALID = 8        # ≥8 只可"强"
BOARD_VALID = 5         # ≥5 只可作板块/子概念表达
OBSERVE_VALID = 2       # 2-4 只只能"观察"

# 不可外推为板块逻辑的驱动 —— 唯一剔除点, 其它模块不得再判一次
EXCLUDED_DRIVERS: frozenset[str] = frozenset({"并购重组"})

# 标签 → 驱动源. 只放**专指词**, 严禁泛词 (项目已有教训: 一个泛词能把任何
# 新闻挂到任何主题上). 命中不了就归"情绪", 由 LLM 叙述层解释, 不影响评级.
_DRIVER_TOKENS: tuple[tuple[str, Driver], ...] = (
    ("重组", "并购重组"),
    ("借壳", "并购重组"),
    ("易主", "并购重组"),
    ("股权转让", "并购重组"),
    ("中标", "产业"),
    ("订单", "产业"),
    ("扩产", "产业"),
    ("涨价", "产业"),
    ("政策", "政策"),
    ("补贴", "政策"),
    ("招标", "政策"),
    ("业绩预增", "业绩"),
    ("净利", "业绩"),
)


def driver_of(tags: Sequence[str]) -> Driver:
    """按标签猜驱动源. 命中不了返回"情绪" —— 不猜, 交给叙述层."""
    joined = "+".join(tags)
    for token, driver in _DRIVER_TOKENS:
        if token in joined:
            return driver
    return "情绪"


def build_drafts(
    board: BoardFlow,
    members: Sequence[Constituent],
    concepts_by_symbol: Mapping[str, Sequence[str]],
    tags_by_symbol: Mapping[str, Sequence[str]],
    *,
    min_cluster: int = 3,
    max_baskets: int = 3,
) -> list[BasketDraft]:
    """按成分股的"额外概念归属"拆子篮子.

    证据分级口径 (P0 的诚实边界):
      C = 该股在涨停池里带归因标签, 且标签命中子概念名或板块名
      D = 仅凭概念归属, 无任何归因 —— 不计入有效样本

    A/B 级 (公告/订单) 需接入公告与订单数据, P0 不具备, 故不产出.
    没有任何可拆的子概念时, 退化为"粗口径"单篮子 (全部 D 级, 必然只能观察),
    并显式标注 —— 宁可说"拆不出来", 也不硬凑.
    """
    buckets: dict[str, list[Constituent]] = {}
    self_bucket: list[Constituent] = []
    for item in members:
        for concept in concepts_by_symbol.get(item.code, ()):
            if not concept:
                continue
            if concept == board.name:
                # 概念名与板块同名: 不是"子概念", 但仍是可用的结构化分组.
                # 直接跳过会逼出"粗口径"兜底, 反而丢掉证据分级 —— 见 _self_draft.
                self_bucket.append(item)
            else:
                buckets.setdefault(concept, []).append(item)

    ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
    chosen = [(name, items) for name, items in ranked if len(items) >= min_cluster]
    chosen = chosen[:max_baskets]

    if not chosen:
        if len(self_bucket) >= min_cluster:
            return [_self_draft(board, _dedup(self_bucket), tags_by_symbol)]
        return [_fallback_draft(board, members, tags_by_symbol)]

    drafts: list[BasketDraft] = []
    for name, items in chosen:
        tags_hit = {i.code: _tags_of(i.code, tags_by_symbol) for i in items}
        drafts.append(BasketDraft(
            parent_board=board.name,
            name=f"{board.name}·{name}",
            members=[i.code for i in items],
            change_pct={i.code: i.change_pct for i in items},
            driver={i.code: driver_of(tags_hit[i.code]) for i in items},
            evidence_level={
                i.code: _grade(name, board.name, tags_hit[i.code]) for i in items
            },
            reason_tags=tags_hit,
        ))
    return drafts


def synthesize(draft: BasketDraft) -> BasketVerdict:
    """合成涨跌幅 + 证据分布 + 驱动一致性 + 评级上限.

    先剔除异源标的再算合成涨跌幅 —— 异源标的对结论的破坏力大于样本量的收益.
    """
    kept = [s for s in draft.members if draft.driver.get(s) not in EXCLUDED_DRIVERS]
    dropped = len(draft.members) - len(kept)

    dist: dict[str, int] = {}
    for s in kept:
        grade = draft.evidence_level.get(s, "D")
        dist[grade] = dist.get(grade, 0) + 1

    valid_n = sum(n for g, n in dist.items() if g in ("A", "B", "C"))
    synth = round(
        sum(draft.change_pct.get(s, 0.0) for s in kept) / max(len(kept), 1), 2,
    )
    top_n = max(dist.values()) if dist else 0

    return BasketVerdict(
        basket=draft,
        synth_change_pct=synth,
        valid_n=valid_n,
        evidence_dist=dist,
        driver_consistency=round(top_n / max(sum(dist.values()), 1), 2),
        rating_cap=rating_cap(valid_n),
        verdict=scope_of(valid_n),
        reason=_reason(len(draft.members), kept, valid_n, dropped),
    )


def rating_cap(valid_n: int) -> RatingCap:
    if valid_n >= STRONG_VALID:
        return "强"
    if valid_n >= BOARD_VALID:
        return "中"
    if valid_n >= OBSERVE_VALID:
        return "观察"
    return "无"


def scope_of(valid_n: int) -> Scope:
    if valid_n >= BOARD_VALID:
        return "板块级"
    return "观察" if valid_n >= OBSERVE_VALID else "个股事件"


# ---- 内部 ----------------------------------------------------------------


def _tags_of(symbol: str, tags_by_symbol: Mapping[str, Sequence[str]]) -> list[str]:
    return list(tags_by_symbol.get(symbol, ()))


def _grade(concept: str, board: str, tags: Sequence[str]) -> Grade:
    joined = "+".join(tags)
    if not joined:
        return "D"
    return "C" if (concept in joined or board in joined) else "D"


def _fallback_draft(
    board: BoardFlow,
    members: Sequence[Constituent],
    tags_by_symbol: Mapping[str, Sequence[str]],
) -> BasketDraft:
    tags_hit = {i.code: _tags_of(i.code, tags_by_symbol) for i in members}
    return BasketDraft(
        parent_board=board.name,
        name=f"{board.name}(粗口径)",
        members=[i.code for i in members],
        change_pct={i.code: i.change_pct for i in members},
        driver={i.code: driver_of(tags_hit[i.code]) for i in members},
        evidence_level={
            i.code: ("C" if tags_hit[i.code] else "D") for i in members
        },
        reason_tags=tags_hit,
    )


def _self_draft(
    board: BoardFlow,
    members: Sequence[Constituent],
    tags_by_symbol: Mapping[str, Sequence[str]],
) -> BasketDraft:
    """概念名与板块同名时的分组: 篮子就是板块本身, 但证据仍逐只分级."""
    tags_hit = {i.code: _tags_of(i.code, tags_by_symbol) for i in members}
    return BasketDraft(
        parent_board=board.name,
        name=board.name,
        members=[i.code for i in members],
        change_pct={i.code: i.change_pct for i in members},
        driver={i.code: driver_of(tags_hit[i.code]) for i in members},
        evidence_level={
            i.code: _grade(board.name, board.name, tags_hit[i.code]) for i in members
        },
        reason_tags=tags_hit,
    )


def _dedup(items: Sequence[Constituent]) -> list[Constituent]:
    seen: set[str] = set()
    out: list[Constituent] = []
    for item in items:
        if item.code in seen:
            continue
        seen.add(item.code)
        out.append(item)
    return out


def _reason(total: int, kept: Sequence[str], valid_n: int, dropped: int) -> str:
    if valid_n == 0:
        head = "无任何有证据成分, 只能作为个股事件披露"
    elif valid_n < OBSERVE_VALID:
        head = f"仅 {valid_n} 只有证据, 只能作为个股事件, 禁止包装成板块"
    elif valid_n < BOARD_VALID:
        head = f"仅 {valid_n} 只有证据, 只能写成观察: {valid_n} 只个股的逻辑"
    else:
        head = f"{valid_n}/{total} 只有证据, 可作板块表达"
    if dropped:
        head += f"; 已剔除 {dropped} 只并购重组类异源标的"
    if len(kept) != total:
        head += f" (合成样本 {len(kept)} 只)"
    return head
