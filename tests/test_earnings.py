from datetime import date

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
