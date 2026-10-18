"""按当前「传导级数」口径复核历史行的强度与幅度.

背景
----
2026-10-18 线上快照 975 第 1 位::

    医用外骨骼康复训练项目纳入多地医保
    → 机器人（关节模组）↑ 强多 +3.4~+8.0%

链路是「医保规划→纳入多地医保→整机采购放量→关节模组需求↑」，**3 跳**。
链条本身不算假（外骨骼整机确实装减速器），但主题的定义是「被同一个驱动因子
驱动」：外骨骼的驱动是医保支付，关节模组的驱动是人形机器人量产。3 跳之后
量级已经小到不影响成分股收入（外骨骼年出货万台级，减速器采购额对双环/绿的
的营收贡献不到 1%），却因为 σ20 借用了人形机器人主题的 4%，被算成贴着单日
上限的涨幅。

代码侧已在 ``news_impact._temper_impact`` 收紧（新评估不会再这样），但**已落库
的历史行不会自动变**。本脚本按同一函数回扫，只动强度/弹性/幅度，不改主题。

判定（与 ``_temper_impact`` 完全一致，直接调它，不重写一遍）
-------------------------------------------------------------
- ``weak and hops >= 2``      → 摘掉该主题（自述"弹性存疑/仅部分重叠"且链条长）
- ``weak or hops >= 3``       → strength 压到 1、elasticity 压到 1.0，重算幅度
- 其余                         → 不动

用法（部署机）::

    cd /deploy/AI_stock/backend
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock HOME=/home/www-data \\
        .venv/bin/python ../scripts/fix_impact_chain.py --dry-run
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock HOME=/home/www-data \\
        .venv/bin/python ../scripts/fix_impact_chain.py

幂等：降权过的行 strength 已是 1，再跑不会再变。
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

from ai_stock.pipeline import news_impact as ni  # noqa: E402

logger = logging.getLogger("fix_impact_chain")

# 只回扫最近这些快照：更早的行早已不在榜单上，改它没有展示价值，
# 而且会让"修复了多少条"这个数字失去意义。
DEFAULT_SNAPSHOTS = 30


def _guard_db_file() -> None:
    """连库前先确认 sqlite 文件存在。

    ``DATABASE_URL=sqlite:///./data/aistock.db`` 是**相对当前目录**的：在项目根
    目录跑一次，就会新建一个空库（`data/aistock.db`，4096 字节），然后才报
    ``no such table: news_items``。事后报错会在工作区留垃圾，所以提前拦。
    """
    path = None
    try:
        from app.core.database import engine

        url = engine.url
        if url.get_backend_name() == "sqlite":
            path = url.database
    except Exception:
        return
    if not path or path == ":memory:":
        return
    if not os.path.exists(path):
        raise SystemExit(
            f"数据库文件不存在：{path}\n"
            "DATABASE_URL 是相对当前目录的，请从 backend/ 目录运行本脚本"
            "（cd /deploy/AI_stock/backend）。"
        )


def _session():
    from ai_stock.pipeline.db_ops import _get_session

    return _get_session()


def _fetch(limit_snapshots: int) -> list[tuple]:
    from sqlalchemy import text

    s = _session()
    try:
        rows = s.execute(text(
            """
            SELECT id, snapshot_id, title, content, industries_json,
                   industry_impacts_json, bull_bear_bias, bias_strength,
                   expected_gain_low, expected_gain_high, horizon, top_stocks_json,
                   debate_summary
            FROM news_items
            WHERE industry_impacts_json IS NOT NULL
              AND industry_impacts_json NOT IN ('', '[]')
              AND snapshot_id >= (
                  SELECT MIN(snapshot_id) FROM (
                      SELECT DISTINCT snapshot_id FROM news_items
                      ORDER BY snapshot_id DESC LIMIT :n
                  )
              )
            ORDER BY snapshot_id DESC, id DESC
            """
        ), {"n": limit_snapshots}).fetchall()
        return [tuple(r) for r in rows]
    except Exception as exc:
        # DATABASE_URL 是相对 CWD 的（sqlite:///./data/aistock.db）。在项目根
        # 目录跑会新建一个空库，然后报 no such table —— 提示比堆栈有用得多。
        if "no such table" in str(exc):
            raise SystemExit(
                "找不到 news_items 表：DATABASE_URL 是相对当前目录的，"
                "请从 backend/ 目录运行本脚本（cd /deploy/AI_stock/backend）。"
            ) from exc
        raise
    finally:
        s.close()


def scan(rows: list[tuple], apply: bool) -> dict:
    stats = {"scanned": 0, "rows_changed": 0, "impacts": 0,
             "downgraded": 0, "dropped": 0, "samples": []}
    s = _session() if apply else None
    try:
        catalog, _ = ni._theme_catalog()
        for (rid, sid, title, content, ind_json, imp_json, bias, strength0,
             low0, high0, horizon, tops_json, reasoning) in rows:
            stats["scanned"] += 1
            try:
                impacts = json.loads(imp_json or "[]")
            except Exception:
                continue
            if not impacts:
                continue

            text_ = f"{title or ''} {content or ''}"
            evidence = ni._theme_evidence(text_, catalog)
            kept, dropped, changed = [], [], False
            for imp in impacts:
                stats["impacts"] += 1
                name = imp.get("name") or ""
                st = int(imp.get("strength") or 1)
                el = float(imp.get("elasticity") or 1.0)
                basis = imp.get("basis") or ""
                direction = imp.get("direction") or "neutral"
                # 命中任意主题 = 落在体系覆盖的赛道内（见 news_impact._temper_impact）
                in_uni = True if evidence is None else bool(evidence)
                keep, st2, el2 = ni._temper_impact(
                    st, el, basis, reasoning or "", in_universe=in_uni,
                )
                if not keep:
                    dropped.append((name, ni._transmission_hops(basis)))
                    stats["dropped"] += 1
                    changed = True
                    continue
                if st2 != st or el2 != el:
                    low, high = ni.calibrate(
                        direction, st2, el2, ni._sigma_for(name), horizon or "1d",
                    )
                    imp = dict(imp, strength=st2, elasticity=round(el2, 2),
                               exp_low=low, exp_high=high)
                    stats["downgraded"] += 1
                    changed = True
                    if len(stats["samples"]) < 12:
                        stats["samples"].append(
                            (sid, (title or "")[:34], name, st, st2, el, el2, low, high)
                        )
                kept.append(imp)

            if not changed:
                continue
            stats["rows_changed"] += 1
            primary = kept[0] if kept else None
            new_bias = primary["direction"] if primary else "neutral"
            new_strength = int(primary["strength"]) if primary else 0
            new_low = primary["exp_low"] if primary else 0.0
            new_high = primary["exp_high"] if primary else 0.0
            new_tops = tops_json if primary else "[]"
            new_ind = json.dumps([i["name"] for i in kept], ensure_ascii=False)

            if apply and s is not None:
                from sqlalchemy import text

                s.execute(text(
                    """
                    UPDATE news_items
                       SET industry_impacts_json = :imp,
                           industries_json = :ind,
                           bull_bear_bias = :bias,
                           bias_strength = :st,
                           expected_gain_low = :low,
                           expected_gain_high = :high,
                           top_stocks_json = :tops
                     WHERE id = :id
                    """
                ), {
                    "imp": json.dumps(kept, ensure_ascii=False),
                    "ind": new_ind,
                    "bias": new_bias,
                    "st": new_strength,
                    "low": float(new_low),
                    "high": float(new_high),
                    "tops": new_tops,
                    "id": rid,
                })
        if apply and s is not None:
            s.commit()
    except Exception:
        if s is not None:
            s.rollback()
        raise
    finally:
        if s is not None:
            s.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="按传导级数口径复核历史强度")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--snapshots", type=int, default=DEFAULT_SNAPSHOTS,
                    help=f"只回扫最近 N 个快照（默认 {DEFAULT_SNAPSHOTS}）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _guard_db_file()
    rows = _fetch(args.snapshots)
    stats = scan(rows, apply=not args.dry_run)

    print(
        f"\n{'[dry-run] ' if args.dry_run else ''}扫描 {stats['scanned']} 行 / "
        f"{stats['impacts']} 个主题：改 {stats['rows_changed']} 行，"
        f"降权 {stats['downgraded']} 个，摘掉 {stats['dropped']} 个"
    )
    for s_, t_, n_, st, st2, el, el2, low, high in stats["samples"]:
        print(f"  [{s_}] {t_:<36} {n_:<18} s{st}·e{el} → s{st2}·e{el2}  {low:+.2f}~{high:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
