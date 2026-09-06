"""Tests for the news board (新闻榜) — 选股流程新闻前 20 落库.

Covers:
- select_top_news: 政策优先 + 时间倒序、截断条数、无标题剔除、不改入参
- save_news_board: 空输入短路、db_ops 调用链 (快照→条目→完成)、异常吞掉
"""

from __future__ import annotations

from ai_stock.pipeline.news_board import (
    _to_news_row,
    dedup_news,
    normalize_title,
    save_news_board,
    select_top_news,
)


def _item(title, time, category="news"):
    return {
        "title": title,
        "content": f"{title} content",
        "source": "东方财富",
        "time": time,
        "category": category,
        "title_hash": title[:6],
    }


# ---------------------------------------------------------------------------
# select_top_news
# ---------------------------------------------------------------------------


def test_select_policy_first_then_time_desc():
    news = [
        _item("old-policy", "2026-08-28 09:00", "policy"),
        _item("newest-news", "2026-08-30 09:00"),
        _item("new-policy", "2026-08-30 08:00", "policy"),
        _item("mid-news", "2026-08-29 12:00"),
    ]
    top = select_top_news(news, top_n=4)
    assert [n["title"] for n in top] == [
        "new-policy", "old-policy", "newest-news", "mid-news",
    ]
    assert [n["rank"] for n in top] == [1, 2, 3, 4]


def test_select_truncates_to_top_n():
    news = [_item(f"n{i}", f"2026-08-30 08:{i:02d}") for i in range(30)]
    top = select_top_news(news, top_n=20)
    assert len(top) == 20
    # 时间倒序: 最新的 08:29 排第 1
    assert top[0]["title"] == "n29"


def test_select_drops_empty_titles_and_keeps_input():
    news = [_item("ok", "2026-08-30 08:00"), {"title": "", "time": "x"}]
    top = select_top_news(news, top_n=20)
    assert len(top) == 1
    # 入参不被修改 (rank 只加在拷贝上)
    assert "rank" not in news[0]


def test_select_empty_input():
    assert select_top_news([], top_n=20) == []
    assert select_top_news(None, top_n=20) == []


# ---------------------------------------------------------------------------
# save_news_board
# ---------------------------------------------------------------------------


def test_save_empty_returns_none():
    assert save_news_board([]) == {"snapshot_id": None, "saved": 0}


def test_save_writes_snapshot_and_items(monkeypatch):
    calls: dict = {}

    from ai_stock.pipeline import db_ops

    monkeypatch.setattr(
        db_ops, "create_snapshot",
        lambda period, total_news, status: calls.update(
            period=period, total=total_news, status=status,
        ) or 42,
    )
    monkeypatch.setattr(
        db_ops, "save_news_items",
        lambda snapshot_id, rows: calls.update(snapshot_id=snapshot_id, rows=rows)
        or len(rows),
    )
    monkeypatch.setattr(
        db_ops, "update_snapshot",
        lambda snapshot_id, status: calls.update(done_status=status) or True,
    )

    news = [_item("policy-a", "2026-08-30 08:00", "policy")] + [
        _item(f"news-{i}", f"2026-08-29 08:{i:02d}") for i in range(25)
    ]
    result = save_news_board(news, top_n=20)

    assert result == {"snapshot_id": 42, "saved": 20}
    assert calls["total"] == 26
    assert calls["snapshot_id"] == 42
    assert calls["done_status"] == "completed"
    assert len(calls["rows"]) == 20
    assert calls["rows"][0]["title"] == "policy-a"
    assert calls["rows"][0]["pub_time"] == "2026-08-30 08:00"


def test_save_swallows_db_errors(monkeypatch):
    from ai_stock.pipeline import db_ops

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_ops, "create_snapshot", boom)
    result = save_news_board([_item("x", "2026-08-30 08:00")])
    assert result == {"snapshot_id": None, "saved": 0}


# ---------------------------------------------------------------------------
# _to_news_row
# ---------------------------------------------------------------------------


def test_to_news_row_maps_time_to_pub_time():
    row = _to_news_row(_item("t", "2026-08-30 08:00", "policy") | {"rank": 3})
    assert row["pub_time"] == "2026-08-30 08:00"
    assert row["category"] == "policy"
    assert row["rank"] == 3
    assert row["bull_bear_bias"] == "neutral"


# ---------------------------------------------------------------------------
# normalize_title / dedup_news (同一新闻多源转发去重)
# ---------------------------------------------------------------------------


def test_normalize_title_ignores_punct_space_and_brackets():
    # 仅标点差异 (同花顺逗号 vs 东财空格) → 同一去重键
    assert normalize_title(
        "工信部：规范动力电池竞争秩序，加强监测"
    ) == normalize_title("工信部：规范动力电池竞争秩序 加强监测")
    # 新浪【标题】正文 vs 其它源纯标题 → 同一去重键
    assert normalize_title(
        "【零售试点城市名单公布】记者3日从商务部获悉"
    ) == normalize_title("零售试点城市名单公布")


def test_dedup_removes_multisource_duplicates_keeps_first():
    news = [
        _item("动力电池竞争秩序，加强监测", "2026-08-30 09:00"),
        _item("动力电池竞争秩序 加强监测", "2026-08-30 08:00"),
    ]
    out = dedup_news(news)
    assert len(out) == 1
    # 保留首次出现者 (调用方已按优先级排序)
    assert out[0]["time"] == "2026-08-30 09:00"


def test_dedup_keeps_rewritten_titles():
    # 措辞改写的相似新闻 (非完全重复) 保守保留, 避免误删不同新闻
    news = [
        _item("华康洁净：中标项目遭废标", "2026-08-30 09:00"),
        _item("华康洁净：9018.65万元中标项目被废标", "2026-08-30 08:00"),
    ]
    assert len(dedup_news(news)) == 2


def test_select_top_news_dedups_then_ranks_contiguous():
    news = [
        _item("动力电池竞争秩序，加强监测", "2026-08-30 09:00"),
        _item("动力电池竞争秩序 加强监测", "2026-08-30 08:00"),
        _item("另一条独立新闻", "2026-08-30 07:00"),
    ]
    top = select_top_news(news, top_n=20)
    assert [n["title"] for n in top] == [
        "动力电池竞争秩序，加强监测", "另一条独立新闻",
    ]
    # 去重在编 rank 之前 → rank 连续无跳号
    assert [n["rank"] for n in top] == [1, 2]
