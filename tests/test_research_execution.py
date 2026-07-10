import numpy as np
import pandas as pd

from quant_signal.research_execution import build_next_open_portfolio, next_bar_targets


def _frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range("2026-01-05", periods=3, tz="UTC")
    close = pd.DataFrame({"A": [10.0, 11.5, 12.5]}, index=idx)
    open_ = pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=idx)
    signal = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=idx)
    return close, open_, signal


def test_next_bar_targets_delay_signal_one_bar() -> None:
    _, open_, signal = _frames()

    targets = next_bar_targets(signal, open_)

    assert targets.iloc[0, 0] == 0.0
    assert targets.iloc[1, 0] == 1.0


def test_next_bar_targets_wait_for_available_open() -> None:
    _, open_, signal = _frames()
    open_.iloc[1, 0] = np.nan

    targets = next_bar_targets(signal, open_)

    assert np.isnan(targets.iloc[1, 0])
    assert targets.iloc[2, 0] == 1.0


def test_portfolio_first_fill_is_next_bar_open_with_costs() -> None:
    close, open_, signal = _frames()

    pf = build_next_open_portfolio(close, open_, signal, fees=0.0005, slippage=0.0005)
    orders = pf.orders.records_readable

    assert len(orders) == 1
    assert orders.iloc[0]["Timestamp"] == close.index[1]
    assert abs(float(orders.iloc[0]["Price"]) - 11.0 * 1.0005) < 1e-9
    assert float(orders.iloc[0]["Fees"]) > 0.0
