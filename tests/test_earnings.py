from datetime import date

import pandas as pd
import pytest

from quant_signal.datafeed.earnings import YFinanceEarnings


class _FakeTicker:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    @property
    def calendar(self) -> dict[str, object]:
        if self._symbol == "MU":
            return {"Earnings Date": [date(2026, 7, 15)]}
        if self._symbol == "BOOM":
            raise RuntimeError("rate limited")
        return {}


def test_next_dates_parses_and_skips_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.earnings as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeTicker)
    out = YFinanceEarnings().next_dates(["MU", "SPY", "BOOM"])
    assert out == {"MU": date(2026, 7, 15)}   # SPY 无日历、BOOM 报错都跳过


class _FakeSurpriseTicker:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    @property
    def earnings_dates(self) -> pd.DataFrame | None:
        if self._symbol == "FRESH":     # 10天前公布, 超预期 +12%
            idx = pd.DatetimeIndex([pd.Timestamp("2026-06-28"), pd.Timestamp("2026-09-30")])
            return pd.DataFrame({"Surprise(%)": [12.0, None]}, index=idx)
        if self._symbol == "STALE":     # 90天前公布, 超出30天窗口
            idx = pd.DatetimeIndex([pd.Timestamp("2026-04-09")])
            return pd.DataFrame({"Surprise(%)": [30.0]}, index=idx)
        return None


def test_recent_surprise_window_and_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.earnings as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeSurpriseTicker)
    out = YFinanceEarnings().recent_surprise(["FRESH", "STALE", "NONE"], now=date(2026, 7, 8))
    assert out == {"FRESH": 12.0}   # 30天窗口内才算; 无数据跳过
