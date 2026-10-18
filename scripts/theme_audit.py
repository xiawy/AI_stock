"""主题体检脚本：种子入库 → 6 道校验 → 输出裁决报告。

用法（部署机）：
    cd /deploy/AI_stock/backend
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock \
        .venv/bin/python ../scripts/theme_audit.py

输出：每个主题一行指标 + 结论（active/shadow/rejected），末尾给汇总。
不达标的会写明原因 —— 这是"不达标直接驳回并说明原因"的落地入口。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 允许从仓库根或 backend 目录运行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from ai_stock.quant import theme_registry as tr  # noqa: E402


def main() -> int:
    reset = "--reset" in sys.argv
    if "--sync" in sys.argv:
        # 只同步关键词/别名/文案进库，保留已有 active/rejected 裁决，也不重跑校验
        res = tr.sync_seed_themes()
        print(f"seed sync: updated={res['updated']} missing={res['missing']}")
        return 0

    seed = tr.reset_seed_themes() if reset else tr.seed_default_themes()
    print(
        f"seed: {'reset+' if reset else ''}created={seed['created']} "
        f"skipped={seed['skipped']}\n"
    )

    results = tr.validate_all()
    results.sort(key=lambda r: (r["status"] != "active", r["name"]))

    header = f"{'主题':<20}{'成分股':>5}{'单票':>6}{'前二':>6}{'内相关':>7}{'离散':>6}{'流动性':>7}{'ST':>6}{'结论':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        m = r.get("metrics") or {}
        name = r["name"]
        pad = 20 - sum(2 if ord(c) > 127 else 1 for c in name)
        print(
            f"{name}{' ' * max(0, pad)}"
            f"{m.get('members', 0):>5}"
            f"{m.get('max_weight', 0):>6.0%}"
            f"{m.get('top2', 0):>6.0%}"
            f"{m.get('internal_corr', 0):>7.2f}"
            f"{m.get('dispersion', 0):>6.2f}"
            f"{m.get('liquidity_ratio', 0):>7.0%}"
            f"{m.get('st_ratio', 0):>6.0%}"
            f"{r['status']:>9}"
        )

    print("\n=== 驳回/影子明细 ===")
    for r in results:
        if r["status"] in ("active",):
            continue
        print(f"[{r['status']}] {r['name']}")
        for reason in r.get("reasons") or []:
            print(f"    - {reason}")
        sts = (r.get("metrics") or {}).get("st_symbols") or []
        if sts:
            print(f"    - ST 命中：{sts}")
        if not r.get("reasons"):
            print(f"    - 历史仅 {r['metrics'].get('history_days')} 根，进影子运行")

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n=== 汇总 ===")
    print(json.dumps(counts, ensure_ascii=False))
    active = [r["name"] for r in results if r["status"] == "active"]
    print(f"active({len(active)}): {', '.join(active)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
