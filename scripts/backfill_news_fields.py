"""历史新闻字段回填（规则模式，不调 LLM）.

背景
----
新闻榜重构（2026-10）给 ``news_items`` 加了 8 个事件维度列，但库里 1.9 万条
历史行的新列全是空值 —— 回看历史快照时整页都是空白。LLM 逐条补 1.9 万条
既不现实也没必要（历史快照的价值是看当时的判断，不是重新判断）。

口径
----
- 只用本地规则：事件类型按关键词打分、重要度按规则公式、主题按别名/关键词/
  成分股名反查。**不调 LLM**，不产生任何 token 成本。
- ``confidence`` 统一写 0.30 并在 ``industry_impacts.basis`` 里标注"历史规则回填"，
  前端据此可以区分"当时 LLM 判断"与"事后补的规则标签"，不冒充新结论。
- ``cluster_id`` 不回填：当时的聚类关系已经丢失，凭标题猜同事件没有意义。
- ``expected_gain_low/high``：有主题时用默认 σ 估算（历史数据算不出当时的
  主题波动率），只作为"量级参考"，不参与任何回测。
- 幂等：只处理 ``event_type = ''`` 的行，可反复执行、可中断续跑。

用法::

    python scripts/backfill_news_fields.py             # 全量回填
    python scripts/backfill_news_fields.py --limit 500 # 分批（每批一次事务）
    python scripts/backfill_news_fields.py --dry-run   # 只统计不写库
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BATCH = 500


def _session():
    from ai_stock.pipeline.db_ops import _get_session

    return _get_session()


def _fetch(limit: int | None) -> list[tuple]:
    from sqlalchemy import text

    sql = (
        "SELECT id, title, content FROM news_items "
        "WHERE COALESCE(event_type, '') = '' ORDER BY id"
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    s = _session()
    try:
        return s.execute(text(sql)).fetchall()
    finally:
        s.close()


def _enrich(row: tuple) -> dict:
    """单条历史新闻 → 要写回的字段值。"""
    import json

    from ai_stock.pipeline import news_impact as ni

    _id, title, content = row
    text_ = "%s %s" % (title or "", (content or "")[:300])
    etype = ni.classify_event(text_)
    direction, strength = ni.rule_direction(text_)
    importance = ni.rule_importance({"title": title, "content": content}, etype)

    themes = ni.align_themes(text_, None, limit=2) if text_.strip() else []
    impacts = []
    if themes:
        for name in themes:
            low, high = ni.calibrate(direction, strength, 1.0, ni.DEFAULT_SIGMA, "1d")
            impacts.append({
                "name": name,
                "direction": direction,
                "strength": strength,
                "elasticity": 1.0,
                "exp_low": low,
                "exp_high": high,
                "basis": "历史规则回填（无 LLM 参与，σ 用默认值）",
            })
    primary = impacts[0] if impacts else None
    return {
        "event_type": etype,
        "importance": round(importance, 2),
        "bias_strength": int(primary["strength"]) if primary else 0,
        "horizon": "1d",
        "confidence": 0.30,
        "industry_impacts_json": json.dumps(impacts, ensure_ascii=False) if impacts else "",
        "industries_json": json.dumps([i["name"] for i in impacts], ensure_ascii=False) if impacts else "",
        "bull_bear_bias": primary["direction"] if primary else "neutral",
        "expected_gain_low": primary["exp_low"] if primary else 0.0,
        "expected_gain_high": primary["exp_high"] if primary else 0.0,
    }


def run(limit: int | None, dry_run: bool) -> None:
    from sqlalchemy import text

    rows = _fetch(limit)
    total = len(rows)
    print("待回填 %d 行%s" % (total, "（dry-run，不写库）" if dry_run else ""))
    if not total:
        return

    done = 0
    typed: dict[str, int] = {}
    with_theme = 0
    s = _session()
    stmt = text(
        "UPDATE news_items SET event_type = :event_type, importance = :importance, "
        "bias_strength = :bias_strength, horizon = :horizon, confidence = :confidence, "
        "industry_impacts_json = :industry_impacts_json, industries_json = :industries_json, "
        "bull_bear_bias = :bull_bear_bias, expected_gain_low = :expected_gain_low, "
        "expected_gain_high = :expected_gain_high WHERE id = :id"
    )
    try:
        for i, row in enumerate(rows, 1):
            vals = _enrich(row)
            typed[vals["event_type"]] = typed.get(vals["event_type"], 0) + 1
            if vals["industry_impacts_json"]:
                with_theme += 1
            if not dry_run:
                s.execute(stmt, {**vals, "id": row[0]})
            done += 1
            if not dry_run and i % BATCH == 0:
                s.commit()
                print("  ... %d/%d" % (i, total))
        if not dry_run:
            s.commit()
    finally:
        s.close()

    print("\n已处理 %d 行" % done)
    print("事件类型分布：%s" % sorted(typed.items(), key=lambda x: -x[1]))
    print("能落到主题：%d 行（%.1f%%）" % (with_theme, 100.0 * with_theme / max(1, done)))
    print("其余为宏观/无明确传导链，前端显示「无对应细分赛道」。")


def main() -> None:
    ap = argparse.ArgumentParser(description="历史新闻字段回填")
    ap.add_argument("--limit", type=int, default=0, help="单次处理行数（0=全量）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()
    run(args.limit or None, args.dry_run)


if __name__ == "__main__":
    main()
