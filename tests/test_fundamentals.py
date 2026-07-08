import pytest

from quant_signal.datafeed.fundamentals import YFinanceFundamentals


class _FakeTicker:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    @property
    def info(self) -> dict[str, object]:
        if self._symbol == "BADCO":
            return {"returnOnEquity": -0.05, "debtToEquity": 450.0}
        if self._symbol == "BOOM":
            raise RuntimeError("rate limited")
        return {"returnOnEquity": 0.25, "debtToEquity": 80.0}


def test_quality_flags_only_for_problem_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.fundamentals as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeTicker)
    out = YFinanceFundamentals().quality_flags(["BADCO", "GOOD", "BOOM"])
    assert out == {"BADCO": "ROE为负/高负债"}   # 健康的与报错的都缺席
