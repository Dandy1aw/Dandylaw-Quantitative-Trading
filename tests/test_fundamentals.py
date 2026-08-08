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
        if self._symbol == "MSFT":
            return {
                "marketCap": 3_800_000_000_000,
                "sector": "Technology",
                "industry": "Software - Infrastructure",
                "shortName": "Microsoft",
                "longBusinessSummary": "Cloud and software platform.",
                "totalRevenue": 250_000_000_000,
                "revenueGrowth": 0.15,
                "earningsGrowth": 0.18,
                "profitMargins": 0.36,
                "returnOnEquity": 0.32,
                "freeCashflow": 80_000_000_000,
                "quoteType": "EQUITY",
            }
        if self._symbol == "NOSECTOR":
            return {"shortName": "Unclassified Corp", "quoteType": "EQUITY"}
        return {"returnOnEquity": 0.25, "debtToEquity": 80.0}


def test_quality_flags_only_for_problem_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.fundamentals as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeTicker)
    out = YFinanceFundamentals().quality_flags(["BADCO", "GOOD", "BOOM"])
    assert out == {"BADCO": "ROE为负/高负债"}   # 健康的与报错的都缺席


def test_profiles_maps_yfinance_fields_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.fundamentals as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeTicker)
    profiles = YFinanceFundamentals().profiles(["MSFT", "BOOM"])

    msft = profiles["MSFT"]
    assert msft.market_cap_usd == 3_800_000_000_000
    assert msft.gics_sector == "Information Technology"
    assert msft.industry == "Software - Infrastructure"
    assert msft.revenue_growth == 0.15
    assert msft.quote_type == "EQUITY"
    assert msft.data_status == "ok"
    assert profiles["BOOM"].data_status == "unavailable"


def test_equity_profile_remains_usable_without_sector_or_market_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quant_signal.datafeed.fundamentals as m

    monkeypatch.setattr(m.yf, "Ticker", _FakeTicker)

    profile = YFinanceFundamentals().profiles(["NOSECTOR"])["NOSECTOR"]

    assert profile.quote_type == "EQUITY"
    assert profile.gics_sector is None
    assert profile.market_cap_usd is None
    assert profile.data_status == "ok"
