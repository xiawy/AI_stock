"""手工刷新新闻榜（只跑榜单，不跑选股流程）。

`macro_event` 会在选股流程里顺带落一份快照，但平时想"立刻看一眼现在的榜单"
就得等下一次流程，或者手工拼一段 python。本脚本把那套动作固定下来：

    get_hot_news(3天) → save_news_board(force=True) → 回读该快照的统计

用法（部署机，必须带服务的 .env：LLM 凭据在里面）::

    cd /deploy/AI_stock/backend
    sudo -u www-data bash -c 'set -a; . /deploy/AI_stock/.env; set +a; \\
        export HOME=/home/www-data PROJECT_ROOT=/deploy/AI_stock; \\
        .venv/bin/python /deploy/AI_stock/scripts/news_board_refresh.py'

    # 不要 LLM（纯规则，省 token；用于验证落库链路）
    ... news_board_refresh.py --no-llm
    # 只看会落什么，不写库
    ... news_board_refresh.py --dry-run

`--force` 语义：绕过 2 小时快照节流，一定新建一份快照。**一天别跑太多次** ——
榜单是给人看的，不是流水；
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def build_llm():
    from ai_stock.default_config import DEFAULT_CONFIG
    from ai_stock.quant.llm_helper import create_quant_llm

    llm = create_quant_llm(DEFAULT_CONFIG)
    if not getattr(llm, "available", False):
        print("⚠️  LLM 不可用（缺凭据？），本次按规则模式落库")
        return None
    return llm


def report(snapshot_id: int) -> None:
    """回读快照，按"看不看得懂"的口径报几个数。"""
    from sqlalchemy import text

    from ai_stock.pipeline.db_ops import _get_session

    s = _get_session()
    try:
        rows = s.execute(text(
            "SELECT rank, title, confidence, industries_json, industry_impacts_json "
            "FROM news_items WHERE snapshot_id = :sid ORDER BY rank"
        ), {"sid": snapshot_id}).fetchall()
    finally:
        s.close()

    llm_votes = sum(1 for r in rows if (r[2] or 0) >= 0.5)
    empty = sum(1 for r in rows if (r[3] or "[]") in ("[]", ""))
    impacts = [i for r in rows for i in json.loads(r[4] or "[]")]
    chain = [i for i in impacts if not (i.get("basis") or "").startswith(("规则", "历史规则"))]
    print(
        f"\n快照 {snapshot_id}: {len(rows)} 条 | LLM 票 {llm_votes} | "
        f"空主题 {empty} | 主题 {len(impacts)}（有传导链 {len(chain)}）"
    )
    for rank, title, conf, inds, _ in rows:
        print(f" {rank:>2} c={conf} {str(title)[:30]:<32} {inds}")


def main() -> int:
    ap = argparse.ArgumentParser(description="手工刷新新闻榜（会新建快照）")
    ap.add_argument("--top", type=int, default=None, help="榜单条数（默认配置值 20）")
    ap.add_argument("--days", type=int, default=3, help="取最近几天新闻")
    ap.add_argument("--no-llm", action="store_true", help="纯规则模式（省 token）")
    ap.add_argument("--dry-run", action="store_true", help="只拉新闻与统计，不写库")
    args = ap.parse_args()

    from ai_stock.pipeline.news_board import save_news_board
    from ai_stock.quant.data_service import get_data_service

    t0 = time.time()
    news = get_data_service().get_hot_news(args.days)
    print(f"取数: {len(news)} 条 / {time.time() - t0:.0f}s")
    if not news:
        print("没有新闻，退出")
        return 1

    if args.dry_run:
        from ai_stock.pipeline import news_impact

        prepared = news_impact.prepare(news)
        print(f"初筛后候选 {len(prepared)} 条（dry-run 不写库）")
        return 0

    llm = None if args.no_llm else build_llm()
    res = save_news_board(news, top_n=args.top, llm=llm, force=True)
    print(f"落库: {res} / 总耗时 {time.time() - t0:.0f}s")
    if res.get("snapshot_id"):
        report(int(res["snapshot_id"]))
    return 0 if res.get("snapshot_id") else 2


if __name__ == "__main__":
    raise SystemExit(main())
