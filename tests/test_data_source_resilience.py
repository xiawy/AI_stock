"""数据源韧性单元测试: push2 粘性主机序 + F10 财务主源替换 + mootdx 负缓存窗口 (离线, 不打真实网络)."""

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

    out = a_stock.get_fundamentals("688111")
    assert "Financial Report Period (REPORT_DATE): 2026-06-30" in out
