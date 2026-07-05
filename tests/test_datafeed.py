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
