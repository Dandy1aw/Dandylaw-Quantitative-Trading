import pytest

from quant_signal.datafeed.fx import fetch_usd_rates


def test_fetch_usd_rates_returns_positive_floats(monkeypatch: pytest.MonkeyPatch) -> None:
    import quant_signal.datafeed.fx as m
    import pandas as pd

    def fake_history(self: object, period: str = "5d") -> pd.DataFrame:
        return pd.DataFrame({"Close": [7.80, 7.84]})

    monkeypatch.setattr(m.yf.Ticker, "history", fake_history)
    rates = fetch_usd_rates({"HKD", "KRW"})
    assert rates == {"HKD": 7.84, "KRW": 7.84}


def test_fetch_usd_rates_usd_is_always_one() -> None:
    rates = fetch_usd_rates({"USD"})
    assert rates == {"USD": 1.0}


def test_fetch_usd_rates_empty_input() -> None:
    assert fetch_usd_rates(set()) == {}
