"""Pipeline-specific data functions: impact news collection & limit-up stocks.

Separated from a_stock.py to keep the main vendor file manageable.
Imported by interface.py and registered in the signal_data category.
"""

from __future__ import annotations

import hashlib
import logging
import time as _time
import uuid
from datetime import datetime, timedelta
from typing import Annotated

import requests as _requests

from .config import get_config

logger = logging.getLogger(__name__)

# Reuse the shared User-Agent and rate-limited getter from a_stock
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def _em_get(url, **kwargs):
    """Thin wrapper: import the real rate-limited _em_get from a_stock at call time."""
    from .a_stock import _em_get as _real_em_get
    return _real_em_get(url, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLICY_KEYWORDS = frozenset({
    "政策", "国务院", "央行", "证监会", "银保监", "发改委", "财政部",
    "工信部", "商务部", "住建部", "部委", "地方政", "监管",
    "降准", "降息", "LPR", "MLF", "逆回购", "专项债", "国债", "补贴",
    "以旧换新", "税收优惠", "减免", "扶持", "产业规划", "指导意见",
    "通知", "办法", "条例", "强制", "禁止", "限制", "鼓励",
})


def classify_news(title: str, content: str) -> str:
    """Classify a news item as 'policy' or 'news' based on keyword matching."""
    text = (title + " " + content).lower()
    hits = sum(1 for kw in _POLICY_KEYWORDS if kw in text)
    return "policy" if hits >= 2 else "news"


def title_hash(title: str) -> str:
    """Deterministic short hash for deduplication."""
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# get_impact_news
# ---------------------------------------------------------------------------


def get_impact_news(
    curr_date: str,
    hours: int = 12,
) -> list[dict]:
    """Collect news from the past *hours* for impact assessment.

    Returns a list of dicts with keys:
        title, content, source, time, url, category, title_hash

    Sources: CLS (财联社), Eastmoney 7x24, Baidu stock (百度股市通).
    Deduplication is by title hash.
    """
    cutoff = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(hours=hours)
    all_items: list[dict] = []
    seen_hashes: set[str] = set()

    # --- Source 1: CLS (财联社快讯) ---
    try:
        cls_url = "https://www.cls.cn/nodeapi/telegraphList"
        cls_params = {"rn": "80", "page": "1"}
        cls_headers = {"User-Agent": _UA, "Referer": "https://www.cls.cn/"}
        r = _requests.get(cls_url, params=cls_params, headers=cls_headers, timeout=10)
        for item in r.json().get("data", {}).get("roll_data", []):
            title = item.get("title", "") or item.get("brief", "")
            if not title:
                continue
            content = item.get("content", "") or item.get("brief", "")
            ctime = item.get("ctime", "")
            pub_time = ""
            if ctime:
                try:
                    pub_dt = datetime.fromtimestamp(int(ctime))
                    pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")
                    if pub_dt < cutoff:
                        continue
                except (ValueError, TypeError, OSError):
                    pub_time = str(ctime)
            h = title_hash(title)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            all_items.append({
                "title": title,
                "content": content,
                "source": "财联社",
                "time": pub_time,
                "url": "",
                "category": classify_news(title, content),
                "title_hash": h,
            })
    except Exception as e:
        logger.warning("CLS impact news fetch failed: %s", e)

    # --- Source 2: Eastmoney 7x24 ---
    try:
        em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        em_params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": "80", "req_trace": str(uuid.uuid4()),
        }
        em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r = _em_get(em_url, params=em_params, headers=em_headers, timeout=10)
        for item in r.json().get("data", {}).get("fastNewsList", []):
            title = item.get("title", "")
            if not title:
                continue
            content = item.get("summary", "")[:300]
            pub_time = item.get("showTime", "")
            if pub_time:
                try:
                    pt = datetime.strptime(pub_time[:16], "%Y-%m-%d %H:%M")
                    if pt < cutoff:
                        continue
                except ValueError:
                    pass
            h = title_hash(title)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            all_items.append({
                "title": title,
                "content": content,
                "source": "东方财富",
                "time": pub_time,
                "url": "",
                "category": classify_news(title, content),
                "title_hash": h,
            })
    except Exception as e:
        logger.warning("Eastmoney impact news fetch failed: %s", e)

    # --- Source 3: Baidu stock (百度股市通) ---
    try:
        baidu_url = "https://finance.pae.baidu.com/api/getbannernews"
        baidu_params = {"page": "1", "pageSize": "50", "type": "0"}
        baidu_headers = {"User-Agent": _UA, "Referer": "https://gushitong.baidu.com/"}
        r = _requests.get(baidu_url, params=baidu_params, headers=baidu_headers, timeout=10)
        for item in r.json().get("data", {}).get("data", []):
            title = item.get("title", "")
            if not title:
                continue
            content = item.get("abstract", "")[:300]
            pub_time = item.get("time", "")
            h = title_hash(title)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            all_items.append({
                "title": title,
                "content": content,
                "source": "百度股市通",
                "time": pub_time,
                "url": item.get("url", ""),
                "category": classify_news(title, content),
                "title_hash": h,
            })
    except Exception as e:
        logger.warning("Baidu impact news fetch failed: %s", e)

    logger.info(
        "Collected %d unique news items for %s (past %dh)",
        len(all_items), curr_date, hours,
    )
    return all_items


# ---------------------------------------------------------------------------
# get_limit_up_stocks
# ---------------------------------------------------------------------------


def get_limit_up_stocks(
    curr_date: str,
    days: int = 7,
) -> list[dict]:
    """Get stocks that hit limit-up in the past *days* trading days.

    Returns a list of dicts with keys:
        code, name, date, reason_tags, zhangfu, huanshou, chengjiaoe, dde

    Reuses the 同花顺 hot-stocks API for each day.
    """
    all_entries: list[dict] = []
    seen: set[str] = set()  # (code, date) dedup

    base_date = datetime.strptime(curr_date, "%Y-%m-%d")
    checked = 0
    calendar_days = 0

    while checked < days and calendar_days < 30:
        dt = base_date - timedelta(days=calendar_days)
        calendar_days += 1
        # Skip weekends
        if dt.weekday() >= 5:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        checked += 1

        try:
            url = (
                f"http://zx.10jqka.com.cn/event/api/getharden/"
                f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Chrome/117.0.0.0 Safari/537.36"
                ),
            }
            r = _requests.get(url, headers=headers, timeout=10)
            data = r.json()
            if data.get("errocode", 0) != 0:
                continue
            for row in data.get("data") or []:
                code = row.get("code", "")
                key = f"{code}_{date_str}"
                if key in seen:
                    continue
                seen.add(key)
                reason = row.get("reason", "")
                tags = [t.strip() for t in reason.split("+") if t.strip()] if reason else []
                all_entries.append({
                    "code": code,
                    "name": row.get("name", ""),
                    "date": date_str,
                    "reason_tags": tags,
                    "zhangfu": row.get("zhangfu", ""),
                    "huanshou": row.get("huanshou", ""),
                    "chengjiaoe": row.get("chengjiaoe", ""),
                    "dde": row.get("ddejingliang", ""),
                })
            # Rate limit: >=0.5s between API calls
            _time.sleep(0.5)
        except Exception as e:
            logger.warning("Limit-up fetch failed for %s: %s", date_str, e)

    logger.info(
        "Collected %d limit-up entries over %d trading days ending %s",
        len(all_entries), days, curr_date,
    )
    return all_entries
