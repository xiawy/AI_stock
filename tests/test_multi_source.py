"""multi_source 统一多源机制单元测试 (离线, 不打真实网络)."""

import pytest

from ai_stock.dataflows import multi_source as ms


@pytest.mark.unit
def test_collect_sources_isolates_exceptions():
    """单源抛错只记入自身 error, 不打断后续源."""

    def boom(**_):
        raise ValueError("source dead")

    fetches = ms.collect_sources(
        [
            ("bad", boom),
            ("good", lambda **_: [{"k": 1}]),
        ],
    )
    assert len(fetches) == 2
    assert not fetches[0].ok
    assert "source dead" in fetches[0].error
    assert fetches[1].ok
    assert fetches[1].records == [{"k": 1}]


@pytest.mark.unit
def test_collect_sources_treats_none_as_empty():
    fetches = ms.collect_sources([("empty", lambda **_: None)])
    assert fetches[0].error is None
    assert fetches[0].records == []
    assert not fetches[0].ok  # 空结果不算成功


@pytest.mark.unit
def test_merge_records_dedupes_by_key_with_priority():
    """同键记录保留先出现者 (源列表顺序即优先级), 且带 _source 溯源."""
    fa = ms.SourceFetch("A", [{"date": "2026-01-01", "item": "x", "v": 1},
                              {"date": "2026-01-01", "item": "y", "v": 2}])
    fb = ms.SourceFetch("B", [{"date": "2026-01-01", "item": "x", "v": 99},
                              {"date": "2026-01-02", "item": "z", "v": 3}])
    rows, used = ms.merge_records([fa, fb], key=lambda r: (r["date"], r["item"]))
    assert [(r["item"], r["v"]) for r in rows] == [("x", 1), ("y", 2), ("z", 3)]
    assert rows[0]["_source"] == "A"
    assert rows[2]["_source"] == "B"
    assert used == ["A", "B"]


@pytest.mark.unit
def test_merge_records_skips_failed_sources_and_applies_limit():
    fa = ms.SourceFetch("A", [{"k": 1}, {"k": 2}])
    fb = ms.SourceFetch("B", [], error="dead")
    rows, used = ms.merge_records([fa, fb], key=lambda r: r["k"], limit=1)
    assert len(rows) == 1
    assert used == ["A"]


@pytest.mark.unit
def test_first_success_skips_to_next_host():
    """主机级容灾: 第一个源抛错/返空时自动尝试下一个."""
    calls = []

    def dead(**_):
        calls.append("dead")
        raise ConnectionError("rejected")

    def alive(**_):
        calls.append("alive")
        return [{"ok": True}]

    got = ms.first_success([("host1", dead), ("host2", alive)])
    assert got is not None
    assert got.source == "host2"
    assert got.records == [{"ok": True}]
    assert calls == ["dead", "alive"]


@pytest.mark.unit
def test_first_success_returns_none_when_all_fail():
    assert ms.first_success([("a", lambda **_: [])]) is None


@pytest.mark.unit
def test_describe_summarizes_sources():
    fetches = [
        ms.SourceFetch("A", [{"k": 1}]),
        ms.SourceFetch("B", [], error="timeout"),
    ]
    assert ms.describe(fetches) == "A(1条) + B(失败)"
