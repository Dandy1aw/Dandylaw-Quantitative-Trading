import numpy as np
import pandas as pd

from quant_signal.strategies.negative_overreaction import confirm_reversal, detect_shock


def _bars(*, structural_trend: bool = True, volume_ratio: float = 2.0) -> pd.DataFrame:
    idx = pd.bdate_range("2025-08-01", periods=210, tz="UTC")
    base = np.linspace(80.0, 100.0, 205) if structural_trend else np.linspace(120.0, 100.0, 205)
    close = np.concatenate([base, [90.0, 91.0, 92.0, 95.0, 96.0]])
    open_ = close.copy()
    open_[-1] = 97.0
    volume = np.full(210, 1_000_000.0)
    volume[205] = volume_ratio * 1_000_000.0
    high = close + 1.0
    low = close - 1.0
    low[205] = 88.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_detect_shock_requires_trend_drop_and_volume() -> None:
    bars = _bars()
    shock = detect_shock("MU", bars, bars.index[205], min_dollar_volume=1_000_000)
    assert shock is not None
    assert shock.drop <= -0.08
    assert shock.volume_ratio >= 1.8

    assert detect_shock("MU", _bars(volume_ratio=1.2), bars.index[205],
                        min_dollar_volume=1_000_000) is None
    assert detect_shock("MU", _bars(structural_trend=False), bars.index[205],
                        min_dollar_volume=1_000_000) is None


def test_confirmation_waits_two_days_and_enters_next_open() -> None:
    bars = _bars()
    shock = detect_shock("MU", bars, bars.index[205], min_dollar_volume=1_000_000)
    assert shock is not None

    confirmation = confirm_reversal(bars, shock)

    assert confirmation is not None
    assert confirmation.confirmed_at >= bars.index[207]
    assert confirmation.entry_at > confirmation.confirmed_at
    assert confirmation.entry_price == float(bars.loc[confirmation.entry_at, "open"])


def test_confirmation_expires_without_recovery() -> None:
    bars = _bars()
    bars.loc[bars.index[206]:, "close"] = [89.0, 88.5, 88.0, 87.0]
    bars.loc[bars.index[206]:, "low"] = [88.0, 87.5, 87.0, 86.0]
    shock = detect_shock("MU", bars, bars.index[205], min_dollar_volume=1_000_000)
    assert shock is not None
    assert confirm_reversal(bars, shock) is None
