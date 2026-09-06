"""数据源韧性单元测试: push2 粘性主机序 + F10 财务主源替换 + mootdx 负缓存窗口 (离线, 不打真实网络)."""

import logging
import time

import pandas as pd
import pytest
import requests as _requests

from ai_stock.dataflows import a_stock, pipeline_data


def _dead_push2(*args, **kwargs):
    raise ConnectionError("push2 blocked")


@pytest.fixture
def _fresh_push2_state(monkeypatch):
    """重置粘性/死主机全局状态, 避免用例间污染."""
    monkeypatch.setattr(a_stock, "_EM_PUSH2_LAST_OK", [0])
    monkeypatch.setattr(a_stock, "_DEAD_HOSTS", set())


# ---------------------------------------------------------------------------
# push2 粘性主机序
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_astock_push2_get_sticky_host(monkeypatch, _fresh_push2_state):
    """主域被拒一次后, 后续请求直达延迟域, 不再重探死主机."""
    urls = []

    def fake_em_get(url, params=None, **kwargs):
        urls.append(url)
        if "push2delay" not in url:
            raise ConnectionError("RemoteDisconnected")
        return "OK"

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)

    assert a_stock._push2_get("/api/qt/stock/get") == "OK"
    assert len(urls) == 2  # 首次: 主域失败 → 回退延迟域
    assert a_stock._push2_get("/api/qt/stock/get") == "OK"
    assert len(urls) == 3  # 第二次: 直达延迟域, 只发一次
    assert "push2delay" in urls[-1]


@pytest.mark.unit
def test_astock_push2_get_sticky_recovers_when_host_dies(monkeypatch, _fresh_push2_state):
    """粘性主机后来也不可达时, 自动全量回退到另一个域."""
    alive = {"delay": False}

    def fake_em_get(url, params=None, **kwargs):
        if "push2delay" in url and not alive["delay"]:
            raise ConnectionError("delay dead too")
        return "OK"

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    a_stock._EM_PUSH2_LAST_OK[0] = 1  # 粘性钉在延迟域

    assert a_stock._push2_get("/api/qt/stock/get") == "OK"
    assert a_stock._EM_PUSH2_LAST_OK[0] == 0  # 已切回主域


@pytest.mark.unit
def test_push2_get_skips_dead_host_without_probing(monkeypatch, _fresh_push2_state):
    """已标记死亡的主机直接跳过, 一次请求只打可达域 (零噪音)."""
    urls = []

    def fake_em_get(url, params=None, **kwargs):
        urls.append(url)
        return "OK"

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    a_stock._DEAD_HOSTS.add("push2.eastmoney.com")

    assert a_stock._push2_get("/api/qt/stock/get") == "OK"
    assert len(urls) == 1  # 未探死主机, 直达延迟域且无警告路径


@pytest.mark.unit
def test_pipeline_push2_get_sticky_host(monkeypatch, _fresh_push2_state):
    """pipeline_data 板块资金流与 a_stock 共用粘性/死主机状态."""
    urls = []

    def fake_em_get(url, params=None, **kwargs):
        urls.append(url)
        if "push2delay" not in url:
            # 真实网络断连抛的就是 requests 的 ConnectionError (死主机标记判据)
            raise _requests.exceptions.ConnectionError("RemoteDisconnected")
        return "OK"

    monkeypatch.setattr(pipeline_data, "_em_get", fake_em_get)

    assert pipeline_data._push2_get("/api/qt/clist/get", {"pn": "1"}) == "OK"
    assert len(urls) == 2  # 首次: 主域失败 → 回退延迟域 (顺带标记死主机)
    assert "push2.eastmoney.com" in a_stock._DEAD_HOSTS
    pipeline_data._push2_get("/api/qt/clist/get", {"pn": "1"})
    assert len(urls) == 3  # 粘性序已全局切到延迟域, 不再探主域
    assert "push2delay" in urls[-1]


# ---------------------------------------------------------------------------
# F10 主财务指标 (mootdx finance 替代源)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_em_main_fin_data_returns_latest_row(monkeypatch):
    def fake_fin_rows(secucode, report_name):
        assert report_name == "RPT_F10_FINANCE_MAINFINADATA"
        if secucode == "600519.SH":
            return [{"REPORT_DATE": "2026-06-30 00:00:00", "EPSJB": 35.57}]
        return []

    monkeypatch.setattr(a_stock, "_em_fin_rows", fake_fin_rows)
    assert a_stock._em_main_fin_data("600519")["EPSJB"] == 35.57
    assert a_stock._em_main_fin_data("000001") == {}


@pytest.mark.unit
def test_get_fundamentals_uses_em_f10_as_primary(monkeypatch):
    """F10 有数据时: 输出带报告期锚点与指标, 且完全不碰 mootdx/独立报告期请求."""
    calls = {"mootdx": 0, "report_date": 0}

    def fake_mootdx(method, **kwargs):
        calls["mootdx"] += 1
        raise AssertionError("EM F10 有数据时不应再探 mootdx")

    def fake_report_date(code):
        calls["report_date"] += 1
        return ""

    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_em_main_fin_data", lambda code: {
        "REPORT_DATE": "2026-06-30 00:00:00", "EPSJB": 35.57, "ROEJQ": 16.75,
    })
    monkeypatch.setattr(a_stock, "_mootdx_call", fake_mootdx)
    monkeypatch.setattr(a_stock, "_em_latest_report_date", fake_report_date)
    monkeypatch.setattr(a_stock, "_push2_get", _dead_push2)
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())
    monkeypatch.setattr(a_stock, "_em_org_basic_info", lambda code: {})

    out = a_stock.get_fundamentals("600519")
    assert "Financial Report Period (REPORT_DATE): 2026-06-30" in out
    assert "EPS (Latest Period): 35.57" in out
    assert "ROE (%): 16.75" in out
    assert calls == {"mootdx": 0, "report_date": 0}


@pytest.mark.unit
def test_get_fundamentals_falls_back_to_mootdx(monkeypatch):
    """F10 主源失败时回退 mootdx 快照 + 独立报告期锚点 (原行为保留)."""
    def dead_f10(code):
        raise ConnectionError("f10 dead")

    fin = pd.DataFrame([{"eps": 1.2, "roe": 9.9}])
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_em_main_fin_data", dead_f10)
    monkeypatch.setattr(a_stock, "_mootdx_call", lambda method, **kw: fin)
    monkeypatch.setattr(a_stock, "_em_latest_report_date", lambda code: "2026-03-31")
    monkeypatch.setattr(a_stock, "_push2_get", _dead_push2)
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())
    monkeypatch.setattr(a_stock, "_em_org_basic_info", lambda code: {})

    out = a_stock.get_fundamentals("000566")
    assert "EPS (Quarterly): 1.2" in out
    assert "ROE (%): 9.9" in out
    assert "Financial Report Period (REPORT_DATE): 2026-03-31" in out


# ---------------------------------------------------------------------------
# mootdx 负缓存窗口: 协议层被拦时窗口内不探测、不逐股刷告警
# ---------------------------------------------------------------------------


@pytest.fixture
def _mootdx_dead_window(monkeypatch):
    """把 mootdx 置于负缓存窗口内 (协议封锁形态), 并重置一次性提示标志."""
    monkeypatch.setattr(a_stock, "_mootdx_client", None)
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", time.time() + 3600)
    monkeypatch.setattr(a_stock, "_mootdx_skip_announced", [False])


@pytest.mark.unit
def test_mootdx_unavailable_predicate(monkeypatch, _mootdx_dead_window):
    """窗口内判不可用; 窗口过期后恢复可探测."""
    assert a_stock._mootdx_unavailable() is True
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", time.time() - 1)
    assert a_stock._mootdx_unavailable() is False


@pytest.mark.unit
def test_get_stock_data_skips_mootdx_when_unavailable(monkeypatch, _mootdx_dead_window):
    """负缓存窗口内 K 线直达新浪: 不探 mootdx, 只留一条一次性提示."""
    def no_mootdx(method, **kwargs):
        raise AssertionError("负缓存窗口内不应探测 mootdx")

    kline = pd.DataFrame({
        "Date": pd.to_datetime(["2026-08-28", "2026-08-29"]),
        "Open": [10.0, 10.5], "High": [10.6, 11.0],
        "Low": [9.9, 10.4], "Close": [10.5, 10.9], "Volume": [1000, 1200],
    })
    monkeypatch.setattr(a_stock, "_mootdx_call", no_mootdx)
    monkeypatch.setattr(a_stock, "_sina_kline_fallback", lambda *a, **k: kline)
    monkeypatch.setattr(
        a_stock, "_supplement_stale_ohlcv_with_sina",
        lambda code, df, *a, **k: (df, False),
    )

    out = a_stock.get_stock_data("688111", "2026-08-01", "2026-08-31")
    assert "sina HTTP (fallback)" in out
    assert "2026-08-29" in out
    assert a_stock._mootdx_skip_announced[0] is True  # 已记一次性 info


@pytest.mark.unit
def test_get_fundamentals_skips_mootdx_fallback_when_unavailable(
    monkeypatch, _mootdx_dead_window
):
    """F10 无数据且窗口内: mootdx 后备同样跳过, 报告期锚点 (HTTP) 照走."""
    def no_mootdx(method, **kwargs):
        raise AssertionError("负缓存窗口内不应探测 mootdx")

    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_em_main_fin_data", lambda code: {})
    monkeypatch.setattr(a_stock, "_mootdx_call", no_mootdx)
    monkeypatch.setattr(a_stock, "_em_latest_report_date", lambda code: "2026-06-30")
    monkeypatch.setattr(a_stock, "_push2_get", _dead_push2)
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())
    monkeypatch.setattr(a_stock, "_em_org_basic_info", lambda code: {})

    out = a_stock.get_fundamentals("688111")
    assert "Financial Report Period (REPORT_DATE): 2026-06-30" in out


# ---------------------------------------------------------------------------
# H1: mootdx 负缓存落盘 —— 同机新进程免于重探整表 (被拦网络实测 ~65s)
# ---------------------------------------------------------------------------


@pytest.fixture
def _negcache_tmp(monkeypatch, tmp_path):
    """把负缓存落盘路径指向 tmp, 并复位一次性加载标志/内存窗口, 模拟干净进程."""
    path = tmp_path / "mootdx-unavailable.until"
    monkeypatch.setattr(a_stock, "_mootdx_negcache_path", lambda: str(path))
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", 0.0)
    monkeypatch.setattr(a_stock, "_mootdx_client", None)
    return path


@pytest.mark.unit
def test_mootdx_negcache_save_load_roundtrip(monkeypatch, _negcache_tmp):
    """写盘的窗口能被"新进程"(内存清零+未加载)读回来, 时间戳一致."""
    until = time.time() + 1234
    a_stock._save_mootdx_negcache(until)
    assert _negcache_tmp.exists()

    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", 0.0)
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    a_stock._load_mootdx_negcache()
    assert a_stock._mootdx_unavailable_until == pytest.approx(until, abs=1)


@pytest.mark.unit
def test_mootdx_negcache_expired_is_discarded(monkeypatch, _negcache_tmp):
    """过期窗口不采纳, 且顺手清掉残留文件 (否则文件永远躺在 cache_dir)."""
    a_stock._save_mootdx_negcache(time.time() - 10)
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    a_stock._load_mootdx_negcache()
    assert a_stock._mootdx_unavailable_until == 0.0
    assert not _negcache_tmp.exists()


@pytest.mark.unit
def test_mootdx_negcache_clear_removes_file(_negcache_tmp):
    """探测成功/reset 时清除落盘窗口."""
    a_stock._save_mootdx_negcache(time.time() + 100)
    assert _negcache_tmp.exists()
    a_stock._clear_mootdx_negcache()
    assert not _negcache_tmp.exists()


@pytest.mark.unit
def test_mootdx_negcache_load_does_not_shorten_existing_window(monkeypatch, _negcache_tmp):
    """磁盘窗口比内存已有窗口早时, 不缩短内存窗口 (取更晚者)."""
    a_stock._save_mootdx_negcache(time.time() + 100)
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", time.time() + 9999)
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    a_stock._load_mootdx_negcache()
    assert a_stock._mootdx_unavailable_until > time.time() + 9000


@pytest.mark.unit
def test_get_mootdx_client_fast_fails_from_persisted_window(monkeypatch, _negcache_tmp):
    """H1 的核心收益: 命中落盘窗口的新进程直接快速失败, 一台服务器都不探."""
    a_stock._save_mootdx_negcache(time.time() + 3600)
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    monkeypatch.setattr(a_stock, "_mootdx_unavailable_until", 0.0)

    def no_probe(*args, **kwargs):
        raise AssertionError("命中落盘负缓存时不应再探测服务器")

    monkeypatch.setattr(a_stock, "_reachable_tdx_servers", no_probe)

    with pytest.raises(RuntimeError, match="不再重试"):
        a_stock._get_mootdx_client()


# ---------------------------------------------------------------------------
# H2: 同花顺一致预期"落空"计数 —— 区分个股无覆盖 vs 整站被反爬/改版
# ---------------------------------------------------------------------------


class _FakeThsResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.encoding = None


@pytest.mark.unit
def test_ths_note_result_warns_once_after_consecutive_misses(caplog):
    """连续落空达阈值 → 只告警一次 (疑似整站被拦), 之后不重复刷屏."""
    with caplog.at_level(logging.WARNING):
        for _ in range(a_stock._THS_CONSECUTIVE_MISS_LIMIT):
            a_stock._ths_note_result(False)
    assert sum("疑似被反爬" in r.getMessage() for r in caplog.records) == 1
    assert a_stock._ths_block_warned[0] is True

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        a_stock._ths_note_result(False)
    assert sum("疑似被反爬" in r.getMessage() for r in caplog.records) == 0


@pytest.mark.unit
def test_ths_note_result_below_limit_stays_silent(caplog):
    """未达阈值(可能只是几只小盘股无覆盖) → 不告警, 避免误报."""
    with caplog.at_level(logging.WARNING):
        for _ in range(a_stock._THS_CONSECUTIVE_MISS_LIMIT - 1):
            a_stock._ths_note_result(False)
    assert sum("疑似被反爬" in r.getMessage() for r in caplog.records) == 0
    assert a_stock._ths_block_warned[0] is False


@pytest.mark.unit
def test_ths_note_result_hit_resets_streak():
    """命中一次即清零连续计数, 并复位告警标志 (间歇性可达不该累积成"封锁")."""
    for _ in range(3):
        a_stock._ths_note_result(False)
    assert a_stock._ths_miss_streak[0] == 3
    a_stock._ths_note_result(True)
    assert a_stock._ths_miss_streak[0] == 0
    assert a_stock._ths_block_warned[0] is False


@pytest.mark.unit
def test_ths_eps_forecast_non_200_counts_as_miss(monkeypatch):
    """整站非 200(限流/拦截) 是硬失败, 记为落空 —— 不再静默当成"个股无覆盖"."""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: _FakeThsResponse(status_code=503, text=""),
    )
    out = a_stock._ths_eps_forecast("002882")
    assert out.empty
    assert a_stock._ths_miss_streak[0] == 1


@pytest.mark.unit
def test_ths_eps_forecast_no_tables_counts_as_miss(monkeypatch):
    """页面 200 但无估值表 → 空表优雅降级, 同时记一次落空供计数判定."""
    monkeypatch.setattr(
        a_stock._requests, "get",
        lambda *a, **k: _FakeThsResponse(200, "<html><body>没有表格</body></html>"),
    )
    out = a_stock._ths_eps_forecast("002882")
    assert out.empty
    assert a_stock._ths_miss_streak[0] == 1


@pytest.mark.unit
def test_ths_eps_forecast_parses_table_and_resets_streak(monkeypatch):
    """正常取到估值表 → 非空, 且清零之前的落空计数."""
    html = (
        "<table><tr><th>年度</th><th>预测机构数</th><th>最小值</th>"
        "<th>每股收益均值</th><th>最大值</th></tr>"
        "<tr><td>2026</td><td>5</td><td>1.0</td><td>1.2</td><td>1.5</td></tr></table>"
    )
    monkeypatch.setattr(
        a_stock._requests, "get", lambda *a, **k: _FakeThsResponse(200, html),
    )
    a_stock._ths_miss_streak[0] = 2
    out = a_stock._ths_eps_forecast("600519")
    assert not out.empty
    assert a_stock._ths_miss_streak[0] == 0


# ---------------------------------------------------------------------------
# H3: push2 行情域被拦时, 行业/上市日期/股本改用东财 F10(datacenter)兜底
# ---------------------------------------------------------------------------


class _FakeEmResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.unit
def test_em_org_basic_info_parses_first_row(monkeypatch):
    monkeypatch.setattr(
        a_stock, "_em_get",
        lambda *a, **k: _FakeEmResponse(
            {"result": {"data": [{"EM2016": "白酒", "LISTING_DATE": "2001-08-27 00:00:00"}]}}
        ),
    )
    org = a_stock._em_org_basic_info("600519")
    assert org["EM2016"] == "白酒"
    assert org["LISTING_DATE"].startswith("2001-08-27")


@pytest.mark.unit
def test_em_org_basic_info_returns_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("datacenter down")

    monkeypatch.setattr(a_stock, "_em_get", boom)
    assert a_stock._em_org_basic_info("600519") == {}


@pytest.mark.unit
def test_em_org_basic_info_returns_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(
        a_stock, "_em_get",
        lambda *a, **k: _FakeEmResponse({"result": {"data": []}}),
    )
    assert a_stock._em_org_basic_info("000001") == {}


@pytest.mark.unit
def test_get_fundamentals_uses_f10_org_info_when_push2_dead(monkeypatch):
    """push2 被拦时: 行业/上市日期取自 F10 公司资料, 股本复用已取的 fin_row(H3)."""
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_em_main_fin_data", lambda code: {
        "REPORT_DATE": "2026-06-30 00:00:00",
        "TOTAL_SHARE": 1256197800,
        "A_FREE_SHARE": 1256190000,
    })
    monkeypatch.setattr(a_stock, "_em_latest_report_date", lambda code: "")
    monkeypatch.setattr(a_stock, "_push2_get", _dead_push2)
    monkeypatch.setattr(a_stock, "_em_org_basic_info", lambda code: {
        "EM2016": "白酒",
        "LISTING_DATE": "2001-08-27 00:00:00",
    })
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())

    def no_mootdx(method, **kwargs):
        raise AssertionError("F10 主源有数据时不应探 mootdx")

    monkeypatch.setattr(a_stock, "_mootdx_call", no_mootdx)

    out = a_stock.get_fundamentals("600519")
    assert "行业: 白酒" in out
    assert "上市日期: 2001-08-27" in out
    assert "总股本: 1256197800" in out
    assert "流通股本(A股): 1256190000" in out


@pytest.mark.unit
def test_get_fundamentals_skips_f10_org_info_when_push2_ok(monkeypatch):
    """push2 正常给出行业/上市日期时, 不再多打一次 F10 公司资料请求(省一次网络)."""
    org_calls = {"n": 0}

    class _Push2Resp:
        def json(self):
            return {"data": {"f127": "白酒", "f189": "20010827", "f84": 1256197800}}

    def count_org(code):
        org_calls["n"] += 1
        return {}

    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})
    monkeypatch.setattr(a_stock, "_em_main_fin_data", lambda code: {})
    monkeypatch.setattr(a_stock, "_em_latest_report_date", lambda code: "")
    monkeypatch.setattr(a_stock, "_push2_get", lambda *a, **k: _Push2Resp())
    monkeypatch.setattr(a_stock, "_em_org_basic_info", count_org)
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())
    monkeypatch.setattr(a_stock, "_mootdx_call", lambda method, **kw: pd.DataFrame())

    out = a_stock.get_fundamentals("600519")
    assert "行业: 白酒" in out
    assert "上市日期: 20010827" in out
    assert org_calls["n"] == 0, "push2 已给全行业+上市日期, 不该再打 F10 公司资料"


# ---------------------------------------------------------------------------
# H4: 跨源拼接前的成交量量纲一致性守卫
# ---------------------------------------------------------------------------


def _ohlcv(volumes):
    n = len(volumes)
    return pd.DataFrame({
        "Date": pd.to_datetime([f"2026-08-{i + 1:02d}" for i in range(n)]),
        "Open": [10.0] * n, "High": [11.0] * n,
        "Low": [9.0] * n, "Close": [10.5] * n,
        "Volume": list(volumes),
    })


@pytest.mark.unit
def test_align_volume_units_scales_down_100x():
    """补充源比主源大 ~100 倍(疑似手当成股) → 缩到主源口径."""
    primary = _ohlcv([1_000_000, 1_200_000, 900_000])
    supplement = _ohlcv([100_000_000, 120_000_000, 90_000_000])
    aligned = a_stock._align_volume_units(primary, supplement)
    assert aligned["Volume"].median() == pytest.approx(1_000_000, rel=0.2)


@pytest.mark.unit
def test_align_volume_units_scales_up_100x():
    """补充源比主源小 ~100 倍 → 放大到主源口径."""
    primary = _ohlcv([100_000_000, 120_000_000, 90_000_000])
    supplement = _ohlcv([1_000_000, 1_200_000, 900_000])
    aligned = a_stock._align_volume_units(primary, supplement)
    assert aligned["Volume"].median() == pytest.approx(100_000_000, rel=0.2)


@pytest.mark.unit
def test_align_volume_units_leaves_same_scale_untouched():
    """仅 2 倍差异是真实量能差(不是单位差) → 原样返回, 不做缩放."""
    primary = _ohlcv([1_000_000, 1_200_000])
    supplement = _ohlcv([2_000_000, 2_400_000])
    aligned = a_stock._align_volume_units(primary, supplement)
    assert aligned["Volume"].median() == pytest.approx(2_200_000, rel=0.2)


@pytest.mark.unit
def test_align_volume_units_no_volume_column_returns_supplement():
    """任一源缺 Volume 列 → 无法比对, 原样返回补充源(交由下游处理)."""
    primary = pd.DataFrame({"Close": [1, 2]})
    supplement = pd.DataFrame({"Close": [3, 4]})
    assert a_stock._align_volume_units(primary, supplement) is supplement


@pytest.mark.unit
def test_merge_ohlcv_applies_volume_alignment():
    """拼接路径确实经过量纲守卫: 100 倍差异被拉平后再合并."""
    primary = _ohlcv([1_000_000, 1_200_000])
    supplement = _ohlcv([100_000_000, 120_000_000])
    merged = a_stock._merge_ohlcv(primary, supplement)
    # 重叠日期保留 supplement(已缩放), 量级应与 primary 一致而非大 100 倍
    assert merged["Volume"].max() < 5_000_000
