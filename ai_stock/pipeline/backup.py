"""Daily ranking backup — export the day's three boards to a dated JSON file.

Once the local time passes ``BACKUP_DAILY_AT`` (23:30), the day's latest
completed snapshot is exported to ``<data-dir>/backups/rankings_<date>.json``
containing:

- 新闻榜 — news items of the snapshot
- 行业榜 — industry rankings of the snapshot
- 热股榜 — stock recommendations of the snapshot

The backup is idempotent (an existing file for the date is kept as-is), runs
in its own thread via the pipeline scheduler, and doubles as the recovery
source should the SQLite file be lost. Files older than the ranking retention
window are removed by the daily cleanup (see backend cleanup service).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKUP_FILENAME_RE = re.compile(r"^rankings_(\d{4}-\d{2}-\d{2})\.json$")


def _backup_dir() -> Path:
    """Resolve the backup directory (``backups/`` next to the SQLite file).

    Uses the same dual-context import strategy as ``db_ops._get_session`` so
    it works both from the backend process and standalone CLI runs.
    """
    for module_path in ("backend.app.core.config", "app.core.config"):
        try:
            from importlib import import_module

            settings = import_module(module_path).get_settings()
            url = getattr(settings, "database_url", "")
        except Exception:
            continue
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                return Path(db_path).parent / "backups"
    # Fallback: relative to the working directory (dev / CLI context).
    return Path("data") / "backups"


def backup_exists_for_date(date_str: str) -> bool:
    """True if a backup file already exists for the given date."""
    return (_backup_dir() / f"rankings_{date_str}.json").is_file()


def backup_today_data(date_str: str | None = None) -> dict:
    """Export the day's latest completed rankings to a dated JSON file.

    Returns a status dict; never raises (failures are logged) so the
    scheduler job cannot spam tracebacks.
    """
    from . import db_ops

    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    target = _backup_dir() / f"rankings_{date_str}.json"
    if target.is_file():
        logger.info("Backup for %s already exists; skipping", date_str)
        return {"status": "skipped", "path": str(target)}

    try:
        core = db_ops.get_snapshot_by_date(date_str)  # 新闻榜 + 热股榜
        industry = db_ops.get_industry_rankings_by_date(date_str)  # 行业榜
    except Exception as exc:
        logger.error("Backup read failed for %s: %s", date_str, exc)
        return {"status": "failed", "error": str(exc)}

    if core is None and industry is None:
        logger.warning("No completed snapshot for %s; nothing to back up", date_str)
        return {"status": "skipped", "reason": "no data for date", "date": date_str}

    payload = {
        "date": date_str,
        "created_at": datetime.now().isoformat(),
        "snapshot": (core or {}).get("snapshot"),
        "news_items": (core or {}).get("news_items", []),  # 新闻榜
        "recommendations": (core or {}).get("recommendations", []),  # 热股榜
        "industry_rankings": (industry or {}).get("rankings", []),  # 行业榜
    }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        tmp.replace(target)
    except Exception as exc:
        logger.error("Backup write failed for %s: %s", date_str, exc)
        return {"status": "failed", "error": str(exc)}

    logger.info(
        "Ranking backup for %s written: %d news, %d industries, %d recommendations",
        date_str,
        len(payload["news_items"]),
        len(payload["industry_rankings"]),
        len(payload["recommendations"]),
    )
    return {"status": "completed", "path": str(target)}


def cleanup_old_backups(keep_days: int) -> int:
    """Delete backup files whose embedded date is older than ``keep_days``.

    Returns the number of files removed. Unknown filenames are left alone.
    """
    backup_dir = _backup_dir()
    if not backup_dir.is_dir():
        return 0

    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for path in backup_dir.iterdir():
        match = _BACKUP_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Failed to remove old backup %s: %s", path, exc)
    if removed:
        logger.info("Removed %d backup file(s) older than %d days", removed, keep_days)
    return removed
