"""漏配扫描：找出"应该有主题却落空"的新闻，并给出补词工单。

为什么需要它
------------
``match_themes`` 门槛统一 2.0（一个别名 3 分，或两个专指关键词各 1 分）之后，
**关键词表里没有那个词** 的赛道新闻会静默留空 —— 新闻明明在讲半导体测试机、
混合键合设备，词表里只有"刻蚀/薄膜沉积"，永远匹配不上。这类问题不会报警，
只能靠回扫数据发现。

三类工单
--------
A. ``stock``  正文提到某主题成分股公司名（≥3 字），却没有主题
              → 规则路径本来就有这路召回，属于真 bug，优先查
B. ``token``  命中 1 个专指关键词（1 分），但不够 2 分
              → 补词工单：把这些词提到 ``aliases``（3 分），或补第二个专指词
C. ``near``   得分 ≥2 却没有主题（且不是 LLM 判的空）
              → 被"仪式性/方向中性"拦掉了，或主题已 retired，逐条看

口径边界：**只看规则行**（``confidence < 0.5``）。LLM 行留空是它明确的判断，
不能拿关键词去覆盖——那正是「AI 短剧出海 → 风电」的成因。

用法::

    # 生产（/deploy/AI_stock 下）
    sudo -u www-data env PROJECT_ROOT=/deploy/AI_stock HOME=/home/www-data \\
        /deploy/AI_stock/backend/.venv/bin/python \\
        /deploy/AI_stock/scripts/theme_miss_scan.py --limit 3000

    # 只看某类工单 / 机器可读
    ... theme_miss_scan.py --limit 3000 --only token
    ... theme_miss_scan.py --limit 3000 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from fix_theme_misfire import (  # noqa: E402
    _session,
    evidence,
    load_stock_names,
    load_tokens,
)

RULE_CONF_MAX = 0.5          # 低于此值是规则降级行（LLM 行 ≥0.55）
TITLE_SAMPLE = 46


def fetch_rows(limit: int, only_empty: bool = True) -> list[tuple]:
    from sqlalchemy import text

    where = "WHERE (industries_json IS NULL OR industries_json IN ('[]','')) " if only_empty else ""
    s = _session()
    try:
        return s.execute(text(
            f"SELECT id, title, content, confidence, industries_json, bull_bear_bias, "
            f"snapshot_id FROM news_items {where}ORDER BY id DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    finally:
        s.close()


def member_hits(text_: str, tokens: dict, names: dict[str, str]) -> list[tuple[str, str]]:
    """(主题, 公司名) —— 正文提到成分股名却没有任何主题。"""
    out: list[tuple[str, str]] = []
    for theme, tk in tokens.items():
        for sym in tk.get("members", ()):  # type: ignore[union-attr]
            nm = names.get(sym, "")
            if nm and len(nm) >= 3 and nm in text_:
                out.append((theme, nm))
                break
    return out


def scan(rows: list[tuple], tokens: dict, names: dict[str, str]) -> dict:
    stock: dict[str, list[tuple]] = defaultdict(list)
    token: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    near: list[tuple] = []
    rule_rows = 0
    # C 类只认最近 2 个快照：更早的行是"当时词表还没有这个词"留下的存量，
    # 不会自动重算，混进来会把工单变成噪音（2026-10-18 实测：补完"测试机"后，
    # 新快照已挂上，但 970/972 的历史行仍在库里留空，一直占用 C 类）。
    latest = sorted({r[6] for r in rows if r[6] is not None}, reverse=True)[:2]

    for rid, title, content, conf, ind_json, bias, sid in rows:
        if (ind_json or "[]") not in ("[]", ""):
            continue
        if (conf or 0.0) >= RULE_CONF_MAX:
            continue                      # LLM 判的空，尊重它的结论
        rule_rows += 1
        text_ = f"{title or ''} {content or ''}"

        for theme, nm in member_hits(text_, tokens, names):
            stock[theme].append((rid, (title or "")[:TITLE_SAMPLE], nm))

        best: list[tuple[float, list[str], str]] = []
        for theme, tk in tokens.items():
            strong, hits, _ = evidence(text_, tk)
            if strong > 0:
                best.append((strong, hits, theme))
        best.sort(reverse=True)

        if best and best[0][0] >= 2.0:
            if sid in latest:
                near.append((rid, (title or "")[:TITLE_SAMPLE], best[0][2], best[0][1]))
            continue
        for strong, hits, theme in best[:2]:
            for hit in hits:
                token[(theme, hit)].append((rid, (title or "")[:TITLE_SAMPLE]))

    return {
        "scanned": len(rows),
        "rule_rows": rule_rows,
        "stock": {k: v for k, v in stock.items()},
        "token": {f"{t}|{h}": v for (t, h), v in token.items()},
        "near": near,
    }


def report(result: dict, *, top: int = 20) -> None:
    print(f"扫描 {result['scanned']} 行空主题，其中规则行 {result['rule_rows']} 行\n")

    print(f"=== A. 提到成分股名却没有主题：{sum(len(v) for v in result['stock'].values())} 条 ===")
    for theme, items in sorted(result["stock"].items(), key=lambda kv: -len(kv[1]))[:top]:
        print(f"  {theme:<20} {len(items):>4} 条  例：{items[0][1]}")
    if not result["stock"]:
        print("  无 —— 这路召回是通的")

    rows = sorted(result["token"].items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for _, v in rows)
    print(f"\n=== B. 差一个词（1 分证据）：{total} 条 / {len(rows)} 个词 ===")
    for key, items in rows[:top]:
        theme, hit = key.split("|", 1)
        print(f"  {hit:<14} {theme:<20} {len(items):>4} 条  例：{items[0][1]}")

    print(f"\n=== C. 得分够了却没挂上：{len(result['near'])} 条（最近 2 个快照；仪式性/中性拦截或主题 retired）===")
    for rid, title, theme, hits in result["near"][:top]:
        print(f"  #{rid} {title}  -> {theme} {hits}")


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻主题漏配扫描（只读）")
    ap.add_argument("--limit", type=int, default=3000, help="回扫最近 N 行新闻")
    ap.add_argument("--only", choices=("stock", "token", "near"), help="只看某一类工单")
    ap.add_argument("--top", type=int, default=20, help="每类最多打印几行")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    tokens = load_tokens()
    symbols = {s for tk in tokens.values() for s in tk.get("members", ())}
    names = load_stock_names(symbols)          # 取不到就退化为不查公司名
    rows = fetch_rows(args.limit)
    result = scan(rows, tokens, names)

    if args.only == "stock":
        result["token"], result["near"] = {}, []
    elif args.only == "token":
        result["stock"], result["near"] = {}, []
    elif args.only == "near":
        result["stock"], result["token"] = {}, {}

    if args.json:
        print(json.dumps(
            {
                "scanned": result["scanned"],
                "rule_rows": result["rule_rows"],
                "stock": {k: [{"id": r[0], "title": r[1], "name": r[2]} for r in v]
                          for k, v in result["stock"].items()},
                "token": {k: [{"id": r[0], "title": r[1]} for r in v]
                          for k, v in result["token"].items()},
                "near": [{"id": r[0], "title": r[1], "theme": r[2], "hits": r[3]}
                         for r in result["near"]],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        report(result, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
