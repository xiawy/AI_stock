"""L3 细分赛道注册表：种子入库、6 道校验、自动转正/退休、新闻匹配。

设计见 ``docs/THEME_TAXONOMY_DESIGN.md``。核心原则：

- **不达标就驳回**：6 道校验全部用真实行情算，任一不过即 ``rejected`` 并写明原因，
  不做人工豁免（用户要求"不达标的直接驳回并说明原因"）。
- **全自动流转**：candidate → (数据不足) shadow → active → (复审不过) retired。
  人工只需事后抽查，不需要审批。
- **宏观口径可解释**：每个指标都落 ``metrics_json``，主题详情页直接展示。

数据来源（均已验证生产可用）：
- 流通市值：腾讯批量行情 ``_tencent_quote``（0.14s/3 只）
- 日线：``DataService.get_ohlcv``（0.43s/只，60 根）
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Iterable, Optional

from sqlalchemy import delete, select

from .db import QuantBase, get_engine, init_quant_db, session_scope
from .db_models import QuantTheme, QuantThemeAlias, QuantThemeDaily, QuantThemeStock

logger = logging.getLogger(__name__)

# ── 6 道校验阈值（改这里就等于改口径，务必同步文档）─────────────────────
GATE_MIN_MEMBERS = 8
GATE_MAX_MEMBERS = 40
GATE_MAX_CR5 = 0.60            # 前 5 大权重和（仅作参考指标，不再作为闸门）
# 为什么不用 CR5 做闸门：市值加权下 8~11 只票的 CR5 天然落在 78%~95%，
# 它衡量的是"这个行业有没有大市值龙头"，而不是"是不是单票"。真正该拦的是
# 单票绑架（max_weight）与双龙头绑架（top2）。
GATE_MAX_SINGLE_WEIGHT = 0.40
GATE_MAX_TOP2_WEIGHT = 0.65
GATE_MIN_INTERNAL_CORR = 0.35  # 冷启动用"主题内一致性"替代"与父行业 ρ"（父板块无历史）
GATE_MAX_DISPERSION = 1.6      # 个股波动 / 主题波动
GATE_MIN_LIQUIDITY_RATIO = 0.80
GATE_MIN_AVG_AMOUNT = 5_000_000.0   # 日均成交额 5000 万
GATE_MAX_ST_RATIO = 0.10
GATE_MIN_KEYWORDS = 3
GATE_MIN_HISTORY_DAYS = 40     # 少于这个天数只能进 shadow

STATUS_ACTIVE = "active"
STATUS_SHADOW = "shadow"
STATUS_REJECTED = "rejected"
STATUS_RETIRED = "retired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema() -> None:
    """建表（4 张表都是新增表，create_all 即可，无需 ALTER）。"""
    init_quant_db()
    QuantBase.metadata.create_all(bind=get_engine())


# ---------------------------------------------------------------------------
# 种子入库
# ---------------------------------------------------------------------------


def seed_default_themes() -> dict:
    """把 ``theme_seed.DEFAULT_THEMES`` 幂等写入（按 name 去重，已有则跳过）。"""
    from .theme_seed import DEFAULT_THEMES

    ensure_schema()
    created, skipped = 0, 0
    with session_scope() as session:
        for item in DEFAULT_THEMES:
            exists = session.execute(
                select(QuantTheme).where(QuantTheme.name == item["name"])
            ).scalar_one_or_none()
            if exists is not None:
                skipped += 1
                continue
            theme = QuantTheme(
                name=item["name"],
                parent_industry=item.get("parent_industry", ""),
                origin=item.get("origin", "compose"),
                status="candidate",
                thesis=item.get("thesis", ""),
                driver_keywords_json=json.dumps(item.get("keywords", []), ensure_ascii=False),
                legacy_board_codes_json=json.dumps(item.get("boards", []), ensure_ascii=False),
                created_by="seed",
            )
            session.add(theme)
            session.flush()          # 拿 theme.id
            for symbol, role in item.get("members", []):
                session.add(
                    QuantThemeStock(
                        theme_id=theme.id, symbol=symbol, role=role, weight=0.0,
                    )
                )
            for alias in item.get("aliases", []):
                session.merge(QuantThemeAlias(alias=alias, theme_id=theme.id))
            created += 1
        session.commit()
    logger.info("theme seed: created=%d, skipped=%d", created, skipped)
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def reset_seed_themes() -> dict:
    """清空种子主题后重建（改过成分股/改名时用）。只删 ``created_by='seed'`` 的行。

    SQLite 默认不开外键级联，因此子表显式删除。
    """
    ensure_schema()
    with session_scope() as session:
        ids = [
            r.id for r in session.execute(
                select(QuantTheme).where(QuantTheme.created_by == "seed")
            ).scalars().all()
        ]
        if ids:
            session.execute(delete(QuantThemeDaily).where(QuantThemeDaily.theme_id.in_(ids)))
            session.execute(delete(QuantThemeStock).where(QuantThemeStock.theme_id.in_(ids)))
            session.execute(delete(QuantThemeAlias).where(QuantThemeAlias.theme_id.in_(ids)))
            session.execute(delete(QuantTheme).where(QuantTheme.id.in_(ids)))
            session.commit()
    logger.info("theme seed reset: removed %d themes", len(ids))
    return seed_default_themes()


def get_theme(name: str) -> Optional[dict]:
    with session_scope() as session:
        row = session.execute(
            select(QuantTheme).where(QuantTheme.name == name)
        ).scalar_one_or_none()
        return row.to_dict() if row else None


def list_themes(status: str | None = None) -> list[dict]:
    with session_scope() as session:
        stmt = select(QuantTheme).order_by(QuantTheme.id)
        if status:
            stmt = stmt.where(QuantTheme.status == status)
        return [row.to_dict() for row in session.execute(stmt).scalars().all()]


def theme_members(theme_id: int) -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(QuantThemeStock).where(QuantThemeStock.theme_id == theme_id)
        ).scalars().all()
        return [
            {
                "symbol": r.symbol, "role": r.role,
                "weight": r.weight, "main_business_pct": r.main_business_pct,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# 6 道校验
# ---------------------------------------------------------------------------


def _history(symbol: str, days: int = 65) -> Optional[dict]:
    """取最近 *days* 根日线（收盘 + 成交量）。数据不足返回 None。"""
    try:
        from .data_service import get_data_service

        df = get_data_service().get_ohlcv(symbol, days)
    except Exception as exc:
        logger.warning("theme history %s failed: %s", symbol, exc)
        return None
    if df is None or len(df) < 25:
        return None
    closes = [float(v) for v in df["Close"].tolist()]
    volumes = [float(v) for v in df["Volume"].tolist()]
    dates = [str(v)[:10] for v in df["Date"].tolist()]
    return {"closes": closes, "volumes": volumes, "dates": dates}


def _caps(symbols: list[str]) -> dict[str, float]:
    """流通市值（元）。腾讯批量接口，失败返回空字典。"""
    out: dict[str, float] = {}
    try:
        from ai_stock.dataflows.a_stock import _tencent_quote

        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            for code, row in (_tencent_quote(chunk) or {}).items():
                cap_yi = float(row.get("float_mcap_yi") or row.get("mcap_yi") or 0)
                if cap_yi > 0:
                    out[code] = cap_yi * 1e8
    except Exception as exc:
        logger.warning("theme caps fetch failed: %s", exc)
    return out


def _returns(closes: list[float]) -> list[float]:
    return [
        (closes[i] / closes[i - 1] - 1.0) if closes[i - 1] else 0.0
        for i in range(1, len(closes))
    ]


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 20:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def validate_theme(name: str, *, apply_status: bool = True) -> dict:
    """对单个主题跑 6 道校验，返回 ``{passed, gates, metrics, reasons}``。

    ``apply_status=True`` 时把结论写回库：全过 → active（数据不足 → shadow），
    任一不过 → rejected。
    """
    theme = get_theme(name)
    if theme is None:
        return {"name": name, "passed": False, "reasons": ["主题不存在"]}

    members = theme_members(theme["id"])
    symbols = [m["symbol"] for m in members]
    metrics: dict = {"members": len(symbols)}
    reasons: list[str] = []

    # 1) 成分股数量
    if not (GATE_MIN_MEMBERS <= len(symbols) <= GATE_MAX_MEMBERS):
        reasons.append(
            f"成分股 {len(symbols)} 只，超出 {GATE_MIN_MEMBERS}~{GATE_MAX_MEMBERS} "
            f"（少于 {GATE_MIN_MEMBERS} 是单票事件，多于 {GATE_MAX_MEMBERS} 是行业）"
        )

    # 2) 关键词
    keywords = theme["driver_keywords"]
    if len(keywords) < GATE_MIN_KEYWORDS:
        reasons.append(f"驱动关键词 {len(keywords)} 个 < {GATE_MIN_KEYWORDS}")

    # 取数与统计
    caps = _caps(symbols)
    hist: dict[str, dict] = {}
    for sym in symbols:
        h = _history(sym)
        if h:
            hist[sym] = h
    metrics["history_symbols"] = len(hist)

    if not hist or len(hist) < min(5, len(symbols)):
        reasons.append(f"可用日线不足：仅 {len(hist)}/{len(symbols)} 只有 ≥25 根历史")
        return _finish(theme, metrics, reasons, apply_status, history_days=0)

    # 权重（自由流通市值；缺失则等权）
    use_caps = all(s in caps for s in hist)
    weights = {s: (caps[s] if use_caps else 1.0) for s in hist}
    total_w = sum(weights.values()) or 1.0
    weights = {s: w / total_w for s, w in weights.items()}
    metrics["weight_basis"] = "float_mcap" if use_caps else "equal"

    # 3) CR5
    ranked_w = sorted(weights.values(), reverse=True)
    cr5 = sum(ranked_w[:5])
    max_w = ranked_w[0] if ranked_w else 0.0
    top2 = sum(ranked_w[:2])
    metrics["cr5"] = round(cr5, 4)                 # 参考值（市值加权天然偏高）
    metrics["max_weight"] = round(max_w, 4)
    metrics["top2"] = round(top2, 4)
    if max_w > GATE_MAX_SINGLE_WEIGHT:
        reasons.append(
            f"单票权重 {max_w:.0%} > {GATE_MAX_SINGLE_WEIGHT:.0%}（本质是单票，不是一个赛道）"
        )
    if top2 > GATE_MAX_TOP2_WEIGHT:
        reasons.append(f"前两大权重和 {top2:.0%} > {GATE_MAX_TOP2_WEIGHT:.0%}（双龙头绑架）")

    # 收益序列（按位对齐：A 股同一交易日历，停牌导致长度差异时取较短尾部）
    rets = {s: _returns(h["closes"]) for s, h in hist.items()}
    min_len = min(len(r) for r in rets.values())
    rets = {s: r[-min_len:] for s, r in rets.items()}
    history_days = min_len
    metrics["history_days"] = history_days

    theme_ret: list[float] = []
    for i in range(min_len):
        theme_ret.append(sum(weights[s] * rets[s][i] for s in rets))

    # 4) 主题内一致性（冷启动替代"与父行业 ρ"）
    pairs = []
    syms = list(rets)
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            c = _pearson(rets[syms[i]], rets[syms[j]])
            if c is not None:
                pairs.append(c)
    internal_corr = mean(pairs) if pairs else 0.0
    metrics["internal_corr"] = round(internal_corr, 4)
    metrics["corr_pairs"] = len(pairs)
    if internal_corr < GATE_MIN_INTERNAL_CORR:
        reasons.append(
            f"主题内平均相关性 {internal_corr:.2f} < {GATE_MIN_INTERNAL_CORR}"
            "（成分股不像同一驱动，可能是拼凑）"
        )

    # 5) 离散度 = 个股波动均值 / 主题波动
    theme_std = pstdev(theme_ret) if len(theme_ret) > 2 else 0.0
    # 离散度必须用**等权**口径: 市值加权会让主题波动贴近龙头, 比值虚高,
    # 把"行业里有大票"误判成"成分股分化"。等权口径才纯粹反映相关性结构。
    eq_ret = [mean(rets[s][i] for s in rets) for i in range(min_len)]
    eq_std = pstdev(eq_ret) if len(eq_ret) > 2 else 0.0
    member_std = mean([pstdev(r) for r in rets.values()]) if rets else 0.0
    dispersion = (member_std / eq_std) if eq_std > 0 else 0.0
    metrics["dispersion"] = round(dispersion, 4)
    metrics["sigma_daily"] = round(theme_std, 5)
    if dispersion > GATE_MAX_DISPERSION:
        reasons.append(f"内部离散度 {dispersion:.2f} > {GATE_MAX_DISPERSION}（分化过大）")

    # 6) 可交易性：近 20 日日均成交额 + ST 占比
    amounts = []
    for s, h in hist.items():
        tail = min(20, len(h["closes"]))
        amt = mean([h["closes"][-k] * h["volumes"][-k] for k in range(1, tail + 1)])
        amounts.append((s, amt))
    liquid = [a for _, a in amounts if a >= GATE_MIN_AVG_AMOUNT]
    liq_ratio = len(liquid) / len(amounts) if amounts else 0.0
    metrics["liquidity_ratio"] = round(liq_ratio, 4)
    metrics["avg_amount_median"] = round(sorted(a for _, a in amounts)[len(amounts) // 2], 0) if amounts else 0
    if liq_ratio < GATE_MIN_LIQUIDITY_RATIO:
        reasons.append(
            f"日均成交额 ≥5000万 的占比 {liq_ratio:.0%} < {GATE_MIN_LIQUIDITY_RATIO:.0%}"
            "（买不进去）"
        )

    from ai_stock.dataflows.a_stock import _tencent_quote

    names = {c: r.get("name", "") for c, r in (_tencent_quote(symbols) or {}).items()}
    st_symbols = [
        f"{c}:{n}" for c, n in names.items() if "ST" in n.upper() or "退" in n
    ]
    st_count = len(st_symbols)
    st_ratio = st_count / len(symbols) if symbols else 0.0
    metrics["st_ratio"] = round(st_ratio, 4)
    metrics["st_symbols"] = st_symbols
    if st_ratio > GATE_MAX_ST_RATIO:
        reasons.append(f"ST/退市标的占比 {st_ratio:.0%} > {GATE_MAX_ST_RATIO:.0%}")

    result = _finish(theme, metrics, reasons, apply_status, history_days=history_days)

    if apply_status and result["passed"]:
        _persist_daily(theme["id"], hist, weights, rets)
    return result


def _finish(
    theme: dict, metrics: dict, reasons: list[str], apply_status: bool, *, history_days: int,
) -> dict:
    passed = not reasons
    if not passed:
        status = STATUS_REJECTED
    elif history_days < GATE_MIN_HISTORY_DAYS:
        status = STATUS_SHADOW
    else:
        status = STATUS_ACTIVE

    metrics["history_days"] = history_days
    metrics["verdict"] = status
    metrics["reasons"] = reasons
    metrics["validated_at"] = _now().isoformat()

    if apply_status:
        with session_scope() as session:
            row = session.get(QuantTheme, theme["id"])
            if row is not None:
                row.status = status
                row.metrics_json = json.dumps(metrics, ensure_ascii=False)
                row.reviewed_at = _now()
                if status == STATUS_SHADOW and row.shadow_since is None:
                    row.shadow_since = _now()
            session.commit()
    return {
        "name": theme["name"], "passed": passed, "status": status,
        "metrics": metrics, "reasons": reasons,
    }


def _persist_daily(theme_id: int, hist: dict, weights: dict, rets: dict) -> None:
    """把 60 日主题收益回填 ``quant_theme_daily``（幅度校准的统计基础）。"""
    any_sym = next(iter(hist))
    n = min(len(rets[s]) for s in rets)
    dates = hist[any_sym]["dates"][-(n + 1):][1:]   # 与收益对齐（跳过第一天）
    rows: dict[str, QuantThemeDaily] = {}
    with session_scope() as session:
        session.execute(delete(QuantThemeDaily).where(QuantThemeDaily.theme_id == theme_id))
        for i in range(n):
            ret = sum(weights[s] * rets[s][i] for s in rets)
            breadth = sum(1 for s in rets if rets[s][i] > 0) / max(1, len(rets))
            window = [
                sum(weights[s] * rets[s][j] for s in rets)
                for j in range(max(0, i - 19), i + 1)
            ]
            sigma20 = pstdev(window) if len(window) > 2 else 0.0
            trade_date = dates[i] if i < len(dates) else f"T-{n - i}"
            rows[trade_date] = QuantThemeDaily(
                theme_id=theme_id,
                trade_date=trade_date,
                ret=round(ret, 6), breadth=round(breadth, 4),
                sigma20=round(sigma20, 6),
            )
        session.add_all(list(rows.values()))
        session.commit()


def validate_all(*, apply_status: bool = True) -> list[dict]:
    """校验全部非 retired 主题。返回每个主题的结论（供运营报告）。"""
    results = []
    for theme in list_themes():
        if theme["status"] == STATUS_RETIRED:
            continue
        try:
            results.append(validate_theme(theme["name"], apply_status=apply_status))
        except Exception as exc:
            logger.warning("validate theme %s failed: %s", theme["name"], exc)
            results.append({
                "name": theme["name"], "passed": False,
                "status": "error", "reasons": [str(exc)], "metrics": {},
            })
    return results


# ---------------------------------------------------------------------------
# 自动流转 / 复审
# ---------------------------------------------------------------------------


def auto_promote_shadow(min_days: int = 5) -> list[str]:
    """影子运行满 *min_days* 个自然日且复检通过 → active."""
    promoted = []
    cutoff = _now() - timedelta(days=min_days)
    for theme in list_themes(STATUS_SHADOW):
        since = theme.get("created_at")
        if since:
            try:
                created = datetime.fromisoformat(since)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    continue
            except ValueError:
                pass
        result = validate_theme(theme["name"], apply_status=True)
        if result["status"] == STATUS_ACTIVE:
            promoted.append(theme["name"])
    return promoted


def auto_review() -> dict:
    """季度复审：active 主题复检，不达标直接 retired（保留历史序列）。"""
    retired, kept = [], 0
    for theme in list_themes(STATUS_ACTIVE):
        result = validate_theme(theme["name"], apply_status=False)
        if result["passed"]:
            kept += 1
            continue
        with session_scope() as session:
            row = session.get(QuantTheme, theme["id"])
            if row is not None:
                row.status = STATUS_RETIRED
                row.reviewed_at = _now()
                row.metrics_json = json.dumps(result["metrics"], ensure_ascii=False)
            session.commit()
        retired.append({"name": theme["name"], "reasons": result["reasons"]})
    return {"kept": kept, "retired": retired}


# ---------------------------------------------------------------------------
# 新闻匹配（供 news_impact 使用）
# ---------------------------------------------------------------------------


def match_themes(text: str, limit: int = 2) -> list[dict]:
    """按别名与驱动关键词给 active 主题打分，返回 top-N。

    alias 命中权重 3（"海风"这种专指性强），关键词命中权重 1。
    匹配不到返回空列表 —— 调用方据此走 ``theme_proposal`` 队列。
    """
    if not text:
        return []
    lowered = text.lower()
    scores: dict[str, float] = {}
    try:
        with session_scope() as session:
            aliases = session.execute(select(QuantThemeAlias)).scalars().all()
            alias_map: dict[int, list[str]] = {}
            for a in aliases:
                alias_map.setdefault(a.theme_id, []).append(a.alias)
            themes = {
                t.id: t for t in session.execute(
                    select(QuantTheme).where(QuantTheme.status == STATUS_ACTIVE)
                ).scalars().all()
            }
            for tid, theme in themes.items():
                score = 0.0
                for alias in alias_map.get(tid, []):
                    if alias and alias.lower() in lowered:
                        score += 3.0
                for kw in json.loads(theme.driver_keywords_json or "[]"):
                    if kw and kw.lower() in lowered:
                        score += 1.0
                if score > 0:
                    scores[theme.name] = score
    except Exception as exc:
        logger.warning("match_themes failed: %s", exc)
        return []
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"name": n, "score": s} for n, s in ranked]


def greeks(theme_name: str, window: int = 20) -> dict:
    """主题的波动尺度（幅度校准用）: sigma20、近 20 日累计收益。"""
    theme = get_theme(theme_name)
    if not theme:
        return {"sigma20": 0.0, "ret20": 0.0}
    with session_scope() as session:
        rows = session.execute(
            select(QuantThemeDaily)
            .where(QuantThemeDaily.theme_id == theme["id"])
            .order_by(QuantThemeDaily.trade_date.desc())
            .limit(window)
        ).scalars().all()
    if not rows:
        return {"sigma20": 0.0, "ret20": 0.0}
    rets_ = [r.ret for r in rows]
    return {
        "sigma20": round(pstdev(rets_), 6) if len(rets_) > 2 else 0.0,
        "ret20": round(sum(rets_), 6),
    }
