"""Re-run the industry ranking (行业榜) step for the latest completed snapshot.

Why this script exists
-----------------------
The pipeline's Step 8 (``_build_industry_ranking``) regenerates the industry
heatmap and attaches leader stocks (领涨 + 板块最相关 + 弹性, not market-cap).
Older snapshots were produced by a commit that only attached leaders to
``rankings[:3]``, so ranks 4-10 ended up with empty ``leader_stocks``. This
script replays Step 8 using the *current* working-tree code and persists the
refreshed ``industry_rankings`` rows **in place** for the target snapshot.

No LLM calls are made: it reuses the already-debated news stored in the DB
(the only fields it needs are composite_score / bull_bear_bias / industries)
and touches the network only for EastMoney board fund-flow + leader quotes.

Usage
-----
    python scripts/rerun_industry_ranking.py [--snapshot-id N] [--top-n 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DB_PATH = BACKEND_DIR / "data" / "aistock.db"

# Pin the absolute DB path before any app/ai_stock import so the script works
# regardless of CWD (the root .env ships a CWD-relative DATABASE_URL).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rerun_industry_ranking")


def load_snapshot_news(snapshot_id: int | None) -> tuple[int, list[dict]]:
    """Load a completed snapshot's news items.

    Returns (snapshot_id, news_dicts). By default the latest completed
    snapshot is used; pass ``snapshot_id`` to target a specific one.
    """
    from ai_stock.pipeline import db_ops
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem

    session = db_ops._get_session()
    if session is None:
        raise RuntimeError("No DB session available — check DATABASE_URL")
    try:
        query = session.query(ImpactSnapshot).filter(ImpactSnapshot.status == "completed")
        if snapshot_id is not None:
            snap = query.filter(ImpactSnapshot.id == snapshot_id).first()
        else:
            snap = query.order_by(ImpactSnapshot.snapshot_time.desc()).first()
        if snap is None:
            raise SystemExit(
                f"No completed snapshot found (snapshot_id={snapshot_id})"
            )
        news = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snap.id)
            .order_by(NewsItem.rank.asc())
            .all()
        )
        logger.info(
            "Snapshot %d (%s, %s): %d debated news loaded",
            snap.id, snap.snapshot_time, snap.period, len(news),
        )
        return snap.id, [n.to_dict() for n in news]
    finally:
        session.close()


def reconstruct_debated_news(news_dicts: list[dict]) -> list[dict]:
    """Rebuild the industry labels the heatmap needs.

    ``primary_industry`` / ``secondary_industry`` are not persisted on
    NewsItem (only the merged ``industries`` list is). Treat industries[0] /
    industries[1] as the primary / secondary attribution — the same source the
    LLM scoring step uses to fill them when labels are missing.
    """
    rebuilt = []
    for n in news_dicts:
        inds = [i.strip() for i in (n.get("industries") or []) if i and i.strip()]
        d = dict(n)
        d["primary_industry"] = inds[0] if inds else ""
        d["secondary_industry"] = inds[1] if len(inds) >= 2 else ""
        rebuilt.append(d)
    return rebuilt


def clear_industry_rankings(snapshot_id: int) -> int:
    """Delete existing industry_rankings rows for a snapshot. Returns count."""
    from ai_stock.pipeline import db_ops
    from ai_stock.pipeline.db_models import IndustryRanking

    session = db_ops._get_session()
    if session is None:
        raise RuntimeError("No DB session available — check DATABASE_URL")
    try:
        deleted = (
            session.query(IndustryRanking)
            .filter(IndustryRanking.snapshot_id == snapshot_id)
            .delete(synchronize_session=False)
        )
        session.commit()
        return int(deleted or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run Step 8 (industry ranking + leader attachment) "
                    "with current code and persist it for the target snapshot.",
    )
    parser.add_argument(
        "--snapshot-id", type=int, default=None,
        help="Target snapshot id (default: latest completed snapshot).",
    )
    parser.add_argument(
        "--top-n", type=int, default=10,
        help="Number of ranked industries to keep (default: 10).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build the rankings but do NOT write to the DB.",
    )
    args = parser.parse_args()

    from ai_stock.pipeline import db_ops
    from ai_stock.pipeline.pipeline import _build_industry_ranking

    snapshot_id, news_dicts = load_snapshot_news(args.snapshot_id)
    debated_news = reconstruct_debated_news(news_dicts)
    if not debated_news:
        logger.error("Snapshot %d has no news items; nothing to rank.", snapshot_id)
        return 1

    # Build with snapshot_id=None so nothing is written yet — we want to
    # inspect the result before replacing the existing rows.
    logger.info("Building industry heatmap + leaders (network: board flow / quotes)...")
    rankings, _industry_leaders = _build_industry_ranking(
        debated_news, snapshot_id=None, top_n=args.top_n,
    )
    if not rankings:
        logger.error("Industry ranking built empty; DB left untouched.")
        return 1

    print("\n=== Industry rankings (rebuilt with current code) ===")
    for row in rankings:
        leaders = row.get("leader_stocks", [])
        names = ", ".join(
            f"{s.get('name', '')}({s.get('leader_label', '')})" for s in leaders
        )
        print(
            f"  #{row.get('rank', 0):>2} {row.get('industry', ''):<12} "
            f"[{row.get('industry_code', ''):<8}] heat={row.get('heat_score', 0):.1f} "
            f"leaders={len(leaders)} :: {names}"
        )

    if args.dry_run:
        print("\n[dry-run] DB not modified.")
        return 0

    deleted = clear_industry_rankings(snapshot_id)
    saved = db_ops.save_industry_rankings(snapshot_id, rankings)
    print(
        f"\nReplaced snapshot {snapshot_id} industry_rankings: "
        f"deleted {deleted}, saved {saved}."
    )

    # Verify from the DB the way the API serves it.
    latest = db_ops.get_latest_industry_rankings()
    if latest and latest.get("snapshot", {}).get("id") == snapshot_id:
        rows = latest.get("rankings", [])
        empty = [r.get("rank") for r in rows if not r.get("leader_stocks")]
        print(
            f"Verified: {len(rows)} rows served, "
            f"{len(rows) - len(empty)} with leaders, empty ranks: {empty or 'none'}."
        )
    else:
        logger.warning("Could not verify — latest snapshot differs from target id.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

