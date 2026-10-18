"""主题成分股季度刷新：剔除 ST/退市 → 重算市值权重 → 复审 → 出报告.

为什么必须定期刷新
------------------
2026-10 首轮体检就查出 4 只成分股已经 ST（南都电源、荃银高科、诺泰生物、易华录）——
种子是按"逻辑上该在这个赛道"写的，但市场会变：戴帽、退市、主业转型、市值结构
漂移。一只 ST 票进主题，不只是收益异常，还会让整个主题的波动率指标失真。

做什么
------
1. **剔除失效票**：ST/*ST/退市整理/停牌无行情 → 从成分股表删除并记录
2. **重算权重**：按最新自由流通市值，同步 ``weight`` 与 ``role``
   （最大者 leader，后 2 名 peripheral，其余 core）
3. **复审**：``auto_review()``，带数据健康闸门与连续失败缓冲
4. **补位提醒**：成员数跌破 8 只的主题只告警、不自动补 —— 补位需要人判断
   "哪只票真的属于这个赛道"，自动补只会把不相关的票塞进来

用法（部署机，周一盘前由 timer 触发）::

    python scripts/theme_refresh.py            # 实际执行
    python scripts/theme_refresh.py --dry-run  # 只看会改什么
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("theme_refresh")

ST_MARKS = ("ST", "*ST", "S*ST", "SST", "退")


def _is_st(name: str) -> bool:
    upper = (name or "").upper().replace(" ", "")
    return any(mark in upper for mark in ST_MARKS)


def _quotes(symbols: list[str]) -> dict[str, dict]:
    from ai_stock.dataflows.a_stock import _tencent_quote

    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            out.update(_tencent_quote(chunk) or {})
        except Exception as exc:
            logger.warning("quote batch failed: %s", exc)
    return out


def refresh(dry_run: bool) -> dict:
    from sqlalchemy import delete

    from ai_stock.quant import theme_registry as tr
    from ai_stock.quant.db import session_scope
    from ai_stock.quant.db_models import QuantThemeStock

    tr.ensure_schema()
    themes = tr.list_themes()
    if not themes:
        return {"themes": 0, "removed": [], "reweighted": 0, "review": {}}

    members: dict[int, list[dict]] = {}
    all_symbols: list[str] = []
    for t in themes:
        rows = tr.theme_members(t["id"])
        members[t["id"]] = rows
        all_symbols.extend(r["symbol"] for r in rows)

    quotes = _quotes(sorted(set(all_symbols)))
    removed: list[dict] = []
    reweighted = 0

    for theme in themes:
        rows = members[theme["id"]]
        alive = []
        for r in rows:
            q = quotes.get(r["symbol"]) or {}
            name = q.get("name") or ""
            cap = float(q.get("float_mcap_yi") or 0.0)
            if not q or not name:
                removed.append({"theme": theme["name"], "symbol": r["symbol"],
                                "name": name, "why": "无行情（停牌/退市/代码失效）"})
                continue
            if _is_st(name):
                removed.append({"theme": theme["name"], "symbol": r["symbol"],
                                "name": name, "why": "ST/退市风险警示"})
                continue
            alive.append({**r, "name": name, "cap": cap})

        if not alive or len(alive) == len(rows) and all(a["cap"] == 0 for a in alive):
            continue
        total = sum(a["cap"] for a in alive) or 0.0
        if total <= 0:
            continue

        alive.sort(key=lambda x: -x["cap"])
        for i, a in enumerate(alive):
            role = "leader" if i == 0 else ("peripheral" if i >= len(alive) - 2 else "core")
            if a["role"] != role or abs(a["weight"] - a["cap"] / total) > 1e-6:
                reweighted += 1
            a["new_role"] = role
            a["new_weight"] = a["cap"] / total

        if dry_run:
            continue
        with session_scope() as session:
            dead = {r["symbol"] for r in rows} - {a["symbol"] for a in alive}
            if dead:
                session.execute(
                    delete(QuantThemeStock).where(
                        QuantThemeStock.theme_id == theme["id"],
                        QuantThemeStock.symbol.in_(list(dead)),
                    )
                )
            for a in alive:
                row = session.get(QuantThemeStock, (theme["id"], a["symbol"]))
                if row is not None:
                    row.weight = round(a["new_weight"], 6)
                    row.role = a["new_role"]
            session.commit()

    review = tr.auto_review(dry_run=dry_run)

    thin = []
    for theme in themes:
        if dry_run:
            continue
        left = len(tr.theme_members(theme["id"]))
        if left and left < tr.GATE_MIN_MEMBERS:
            thin.append({"theme": theme["name"], "members": left})

    return {
        "themes": len(themes),
        "removed": removed,
        "reweighted": reweighted,
        "review": review,
        "thin": thin,
        "dry_run": dry_run,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="主题成分股季度刷新")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()

    res = refresh(args.dry_run)
    print("主题 %d 个%s" % (res["themes"], "（dry-run）" if res["dry_run"] else ""))

    if res["removed"]:
        print("\n剔除 %d 只：" % len(res["removed"]))
        for r in res["removed"]:
            print("  - %-24s %s %-8s %s" % (r["theme"], r["symbol"], r["name"], r["why"]))
    else:
        print("\n无失效成分股。")

    print("\n权重/角色更新 %d 条" % res["reweighted"])

    rv = res["review"]
    if rv.get("aborted"):
        print("\n复审跳过：%s —— 数据源疑似异常，不写任何状态。" % rv["aborted"])
    else:
        print("\n复审：%d 个保持 active，%d 个警告，%d 个退休"
              % (rv.get("kept", 0), len(rv.get("warned") or []), len(rv.get("retired") or [])))
        for w in rv.get("warned") or []:
            print("  ! %-24s %s（streak=%s）" % (w["name"], "；".join(w["reasons"])[:70],
                                                 w.get("streak", "?")))
        for r in rv.get("retired") or []:
            print("  x %-24s %s" % (r["name"], "；".join(r["reasons"])[:70]))

    if res.get("thin"):
        print("\n成员不足（需人工补位，不自动补）：")
        for t in res["thin"]:
            print("  - %-24s 仅 %d 只" % (t["theme"], t["members"]))


if __name__ == "__main__":
    main()
