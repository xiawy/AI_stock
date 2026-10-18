"""新闻主题误配修复 / 诊断.

背景
----
``match_themes`` 是子串匹配，主题关键词表里只要有一个跨行业通用词（"出海"、
"订单"、"涨价"…），任何新闻都能蹭上分。2026-10-18 线上就出现过::

    国金证券：当前AI短剧出海仍处渗透率低位…
    → 传媒（AI应用）+ 风电（出海）

"风电（出海）" 的关键词里有裸词"出海"，"AI短剧出海" 命中 1 分；LLM 只给了
1 个主题，``align_themes`` 用 limit=2 还差一个空位，补位时用的门槛又是 1.0，
于是这个 1 分的泛词命中被当成第二个主题写进了库。

代码侧已修（关键词表清掉泛词 + 补位门槛统一 2.0 + 泛词记 0 分），但**已经落库
的历史行不会自动变**。本脚本按同一口径回扫，把"完全由泛词支撑"的主题摘掉。

判定口径（只看证据，不改 LLM 的判断）
------------------------------------
对每个 ``industry_impact``：
- strong = 该主题的**专指**别名 ×3 + 专指关键词 ×1
- generic = 只命中泛词（``GENERIC_TOKENS``）
- ``strong == 0 且 generic > 0`` → 该主题的命中完全靠泛词，摘掉；
- 其余一律保留（LLM 给的正确主题往往在正文里一个关键词都不出现，不能按
  关键词重算，那样会把对的也删掉）。

摘掉后重算 industries / bull_bear_bias / bias_strength / expected_gain_*，
若一条主题都不剩，顺带清空 top_stocks（不然会出现"没有行业却挂着风电个股"）。

用法（部署机）::

    cd /deploy/AI_stock/backend
    # 1) 先把种子里的关键词同步进库（清掉裸词"出海"）
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock HOME=/home/www-data \\
        .venv/bin/python ../scripts/theme_audit.py --sync
    # 2) 诊断某条新闻为什么命中某个主题（不写库）
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock HOME=/home/www-data \\
        .venv/bin/python ../scripts/fix_theme_misfire.py --explain "国金证券：当前AI短剧出海仍处渗透率低位"
    # 3) 干跑看影响面
    ... fix_theme_misfire.py --dry-run
    # 4) 真跑
    ... fix_theme_misfire.py

幂等：摘过的行再跑不会重复处理（摘完 strong==0 且 generic 仍存在，但 impacts
里已经没有那个主题了）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from ai_stock.quant.theme_registry import _token_hit  # noqa: E402
from ai_stock.quant.theme_seed import GENERIC_TOKENS  # noqa: E402

BATCH = 300

# 2026-10-18 从种子里摘掉的词（当天之前写进库的历史行，命中的就是这些）。
# 修复历史行时它们按"泛词"算：库里已经查不到了（同步过），只能在这里留一份
# 快照，否则老行会呈现"既没有专指证据、也没有泛词证据"的样子而被放过。
RETIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "风电（出海）": ("出海", "海外订单"),
    "医药（创新药出海）": ("出海", "FDA"),
    "化工（氟化工）": ("配额", "涨价"),
    "军工（导弹链）": ("总装",),
    "家电（清洁电器）": ("代工", "海外收入"),
    "农业（种业转基因）": ("商业化推广",),
    "建筑（水利投资）": ("重大项目",),
    "油服（钻井/工程）": ("资本开支", "油价", "原油", "布伦特", "wti", "opec"),
    "黄金（矿企弹性）": ("避险", "降息", "实际利率"),
}


def _session():
    """用 quant 的 engine（绝对路径 = <repo>/backend/data/aistock.db）。

    不用 ``pipeline.db_ops._get_session``：它走 backend settings 的相对路径
    ``sqlite:///./data/aistock.db``，在仓库根目录跑会连到一个空的 ./data 库，
    报 "no such table: quant_theme"。生产上两份配置指向同一个文件，用绝对
    路径的 engine 更稳。
    """
    from sqlalchemy.orm import Session

    from ai_stock.quant.db import get_engine

    return Session(get_engine())


def load_tokens() -> dict[str, dict]:
    """从库里取每个 active 主题的关键词/别名/成分股（库是匹配时的真实口径）。"""
    from sqlalchemy import text

    s = _session()
    try:
        rows = s.execute(text(
            "SELECT t.name, t.driver_keywords_json FROM quant_theme t "
            "WHERE t.status = 'active'"
        )).fetchall()
        aliases: dict[str, list[str]] = {}
        for name, alias in s.execute(text(
            "SELECT t.name, a.alias FROM quant_theme_alias a "
            "JOIN quant_theme t ON t.id = a.theme_id WHERE t.status = 'active'"
        )).fetchall():
            aliases.setdefault(name, []).append(alias)
        members: dict[str, set[str]] = {}
        for name, symbol in s.execute(text(
            "SELECT t.name, s.symbol FROM quant_theme_stock s "
            "JOIN quant_theme t ON t.id = s.theme_id WHERE t.status = 'active'"
        )).fetchall():
            members.setdefault(name, set()).add(symbol)
    finally:
        s.close()
    out: dict[str, dict] = {}
    for name, kw_json in rows:
        out[name] = {
            "keywords": json.loads(kw_json or "[]"),
            "aliases": aliases.get(name, []),
            "members": members.get(name, set()),
        }
    return out


def evidence(text: str, tokens: dict, retired: tuple[str, ...] = ()) -> tuple[float, list[str], list[str]]:
    """返回 (专指分, 命中的专指 token, 命中的泛词)。"""
    lowered = (text or "").lower()
    strong, strong_hits, generic_hits = 0.0, [], []
    for alias in tokens.get("aliases", []):
        if not alias:
            continue
        if alias.lower() in GENERIC_TOKENS:
            if _token_hit(alias, lowered):
                generic_hits.append(alias)
            continue
        if _token_hit(alias, lowered):
            strong += 3.0
            strong_hits.append(alias)
    for kw in tokens.get("keywords", []):
        if not kw:
            continue
        if kw.lower() in GENERIC_TOKENS:
            if _token_hit(kw, lowered):
                generic_hits.append(kw)
            continue
        if _token_hit(kw, lowered):
            strong += 1.0
            strong_hits.append(kw)
    for tok in retired:
        if _token_hit(tok, lowered):
            generic_hits.append(f"{tok}(已摘)")
    return strong, strong_hits, generic_hits


def explain(text: str) -> int:
    tokens = load_tokens()
    print(f"标题/正文：{text[:80]}\n")
    rows = []
    for name, tk in tokens.items():
        strong, sh, gh = evidence(text, tk, RETIRED_TOKENS.get(name, ()))
        generic_score = float(len(gh))
        total = strong + generic_score
        if total <= 0:
            continue
        rows.append((total, name, strong, sh, gh))
    rows.sort(reverse=True)
    if not rows:
        print("没有任何主题命中（口径 min_score=2.0 时落不到主题）")
        return 0
    print(f"{'主题':<22}{'总分':>5}{'专指分':>7}  专指证据 / 泛词")
    for total, name, strong, sh, gh in rows:
        pad = 22 - sum(2 if ord(c) > 127 else 1 for c in name)
        flag = "  ← 全靠泛词" if strong == 0 and gh else ""
        print(
            f"{name}{' ' * max(0, pad)}{total:>5.0f}{strong:>7.0f}  "
            f"{'/'.join(sh) or '-'} | {'/'.join(gh) or '-'}{flag}"
        )
    print("\n说明：专指分 ≥2 才算命中；「全靠泛词」的行在当前代码下不会再被写入。")
    return 0


def _fetch(limit: int | None) -> list[tuple]:
    from sqlalchemy import text

    sql = (
        "SELECT id, title, content, industry_impacts_json, top_stocks_json "
        "FROM news_items "
        "WHERE COALESCE(industry_impacts_json, '') NOT IN ('', '[]') "
        "ORDER BY id DESC"
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    s = _session()
    try:
        return s.execute(text(sql)).fetchall()
    finally:
        s.close()


def repair(rows: list[tuple], tokens: dict, dry_run: bool) -> dict:
    from sqlalchemy import text

    stats = {"scanned": 0, "changed": 0, "dropped_themes": 0, "samples": []}
    s = _session() if not dry_run else None
    try:
        for rid, title, content, impacts_json, top_stocks in rows:
            stats["scanned"] += 1
            try:
                impacts = json.loads(impacts_json or "[]")
            except Exception:
                continue
            if not impacts:
                continue
            text_ = f"{title or ''} {content or ''}"
            kept, dropped = [], []
            for imp in impacts:
                name = imp.get("name") or ""
                tk = tokens.get(name)
                if tk is None:              # 主题已下线/改名，不动
                    kept.append(imp)
                    continue
                strong, _, generic = evidence(text_, tk, RETIRED_TOKENS.get(name, ()))
                if strong <= 0 and generic:
                    dropped.append((name, generic))
                else:
                    kept.append(imp)
            if not dropped:
                continue
            stats["changed"] += 1
            stats["dropped_themes"] += len(dropped)
            if len(stats["samples"]) < 10:
                stats["samples"].append(
                    (rid, (title or "")[:50], [f"{n}←{'/'.join(g)}" for n, g in dropped])
                )
            if dry_run:
                continue
            primary = kept[0] if kept else None
            # 被摘主题的成分股不能继续留在 top_stocks 里（会出现"只剩传媒却挂着
            # 大金重工"）；只保留仍属于某个保留主题的票。
            keep_syms: set[str] = set()
            drop_syms: set[str] = set()
            for imp in kept:
                keep_syms |= tokens.get(imp.get("name") or "", {}).get("members", set())
            for name, _ in dropped:
                drop_syms |= tokens.get(name, {}).get("members", set())
            tops = top_stocks or "[]"
            if dropped:
                try:
                    stocks = json.loads(tops)
                except Exception:
                    stocks = []
                if isinstance(stocks, list):
                    kept_stocks = [
                        st for st in stocks
                        if not (
                            isinstance(st, dict)
                            and (st.get("code") or st.get("symbol")) in (drop_syms - keep_syms)
                        )
                    ]
                    tops = json.dumps(kept_stocks, ensure_ascii=False)
            params = {
                "imp": json.dumps(kept, ensure_ascii=False),
                "inds": json.dumps([i.get("name") for i in kept], ensure_ascii=False),
                "bias": primary.get("direction", "neutral") if primary else "neutral",
                "strength": int(primary.get("strength", 0)) if primary else 0,
                "low": float(primary.get("exp_low", 0.0)) if primary else 0.0,
                "high": float(primary.get("exp_high", 0.0)) if primary else 0.0,
                "tops": tops if kept else "[]",
                "id": rid,
            }
            s.execute(text(
                "UPDATE news_items SET industry_impacts_json = :imp, "
                "industries_json = :inds, bull_bear_bias = :bias, "
                "bias_strength = :strength, expected_gain_low = :low, "
                "expected_gain_high = :high, top_stocks_json = :tops "
                "WHERE id = :id"
            ), params)
        if s is not None:
            s.commit()
    finally:
        if s is not None:
            s.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻主题误配修复/诊断")
    ap.add_argument("--explain", metavar="TEXT", help="诊断这段文本为什么命中某主题")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--limit", type=int, default=None, help="最多扫描多少行（倒序）")
    args = ap.parse_args()

    if args.explain:
        return explain(args.explain)

    tokens = load_tokens()
    if not tokens:
        print("库里没有 active 主题，先跑 theme_audit.py --sync")
        return 1
    rows = _fetch(args.limit)
    stats = repair(rows, tokens, args.dry_run)
    print(
        f"{'[dry-run] ' if args.dry_run else ''}扫描 {stats['scanned']} 行，"
        f"需修复 {stats['changed']} 行，摘掉 {stats['dropped_themes']} 个主题"
    )
    for rid, title, dropped in stats["samples"]:
        print(f"  #{rid} {title} → 摘掉 {', '.join(dropped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
