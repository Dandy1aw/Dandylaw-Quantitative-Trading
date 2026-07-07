from datetime import date

import pandas as pd
import pytest

from quant_signal.datafeed.yf_source import YFinanceSource


def fake_yf_download(*args: object, **kwargs: object) -> pd.DataFrame:
    """模拟 yf.download(group_by='ticker') 两只票的返回结构。"""
    idx = pd.date_range("2026-01-05", periods=3, freq="B")
    cols = pd.MultiIndex.from_product(
        [["SPY", "QQQ"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(1.0, index=idx, columns=cols)
    for t in ("SPY", "QQQ"):
        df[(t, "Volume")] = 2_000_000
    return df


def test_fetch_daily_bars_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.yf_source as m

    monkeypatch.setattr(m.yf, "download", fake_yf_download)
    out = YFinanceSource().fetch_daily_bars(
        ["SPY", "QQQ"], date(2026, 1, 5), date(2026, 1, 8)
    )
    assert out.index.names == ["ticker", "ts"]
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert set(out.index.get_level_values("ticker")) == {"SPY", "QQQ"}
    assert str(out.index.get_level_values("ts").tz) == "UTC"
    assert len(out) == 6


def test_fetch_intraday_bars_handles_empty_single_ticker_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单标的请求时，若 yfinance 对该时段没有任何数据（完全空表，无列），不能崩溃。"""
    import quant_signal.datafeed.yf_source as m

    def fake_empty_download(*args: object, **kwargs: object) -> pd.DataFrame:
        return pd.DataFrame()

    monkeypatch.setattr(m.yf, "download", fake_empty_download)
    out = YFinanceSource().fetch_intraday_bars(["000660.KS"], lookback_days=1)
    assert out.empty
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_fetch_live_price_uses_prepost_and_returns_last_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现价取含盘前/盘后的最新收盘：必须以 prepost=True 请求，返回最后一根 close。"""
    import quant_signal.datafeed.yf_source as m

    captured: dict[str, object] = {}

    def fake_download(*args: object, **kwargs: object) -> pd.DataFrame:
        captured.update(kwargs)
        idx = pd.date_range("2026-07-02 08:00", periods=3, freq="5min", tz="UTC")
        return pd.DataFrame(
            {"Open": [1.0, 1.0, 1.0], "High": [1.0, 1.0, 1.0], "Low": [1.0, 1.0, 1.0],
             "Close": [10.0, 11.0, 12.34], "Volume": [1, 1, 1]},
            index=idx,
        )

    monkeypatch.setattr(m.yf, "download", fake_download)
    price = YFinanceSource().fetch_live_price("AAPL")
    assert captured.get("prepost") is True   # 盘前盘后开关必须打开
    assert price == 12.34                     # 取最后一根 5min 收盘


def test_fetch_live_price_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.yf_source as m

    monkeypatch.setattr(m.yf, "download", lambda *a, **k: pd.DataFrame())
    assert YFinanceSource().fetch_live_price("AAPL") is None


def test_fetch_live_price_handles_multiindex_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """yf.download([单票], group_by='ticker') 会返回带 ticker 层的 MultiIndex 列，
    单票归一化必须能剥掉 ticker 层，否则取不到 close（返回 None）。"""
    import quant_signal.datafeed.yf_source as m

    def fake_download(*args: object, **kwargs: object) -> pd.DataFrame:
        idx = pd.date_range("2026-07-02 19:45", periods=2, freq="5min", tz="UTC")
        cols = pd.MultiIndex.from_product(
            [["MU"], ["Open", "High", "Low", "Close", "Volume"]]
        )
        return pd.DataFrame(
            [[1.0, 1.0, 1.0, 60.0, 1.0], [1.0, 1.0, 1.0, 61.5, 1.0]],
            index=idx, columns=cols,
        )

    monkeypatch.setattr(m.yf, "download", fake_download)
    assert YFinanceSource().fetch_live_price("MU") == 61.5


def test_fetch_live_prices_batches_all_tickers_in_one_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quant_signal.datafeed.yf_source as m

    calls: list[tuple[object, dict[str, object]]] = []

    def fake_download(tickers: object, **kwargs: object) -> pd.DataFrame:
        calls.append((tickers, kwargs))
        return fake_yf_download()

    monkeypatch.setattr(m.yf, "download", fake_download)
    prices = YFinanceSource().fetch_live_prices(["SPY", "QQQ"])

    assert len(calls) == 1
    assert calls[0][0] == ["SPY", "QQQ"]
    assert calls[0][1].get("prepost") is True
    assert prices == {"SPY": 1.0, "QQQ": 1.0}
