import numpy as np
import pandas as pd

from quant_signal.overreaction_backtest import summarize_trades, simulate_trade
from quant_signal.strategies.negative_overreaction import ReversalConfirmation, ShockEvent


def _setup(future_close: list[float]) -> tuple[pd.DataFrame, ShockEvent, ReversalConfirmation]:
    idx = pd.bdate_range("2026-01-01", periods=len(future_close), tz="UTC")
    close = np.array(future_close, dtype=float)
    bars = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": 1_000_000}, index=idx,
    )
    shock = ShockEvent("MU", idx[0], 100.0, 90.0, 88.0, 3.0, -0.10, 2.0)
    confirmation = ReversalConfirmation("MU", idx[0], idx[1], idx[2], float(close[2]), 88.0)
    return bars, shock, confirmation


def test_simulate_trade_exits_next_open_after_stop_close() -> None:
    bars, shock, confirmation = _setup([90, 92, 94, 87, 86, 85])
    trade = simulate_trade(bars, shock, confirmation)
    assert trade.exit_reason == "stop"
    assert trade.exit_at == bars.index[4]
    assert trade.exit_price == 86.0
    assert trade.max_adverse_excursion < 0


def test_simulate_trade_exits_next_open_after_gap_repair() -> None:
    bars, shock, confirmation = _setup([90, 92, 94, 98, 99, 100])
    trade = simulate_trade(bars, shock, confirmation)
    assert trade.exit_reason == "repair"
    assert trade.exit_at == bars.index[4]


def test_simulate_trade_time_exits_after_40_sessions() -> None:
    bars, shock, confirmation = _setup([90, 92, 94] + [95] * 45)
    trade = simulate_trade(bars, shock, confirmation, max_hold=40)
    assert trade.exit_reason == "time"
    assert trade.exit_at == bars.index[43]


def test_summary_reports_trade_quality() -> None:
    win_bars, shock, confirmation = _setup([90, 92, 94, 98, 99, 100])
    loss_bars, shock2, confirmation2 = _setup([90, 92, 94, 87, 86, 85])
    summary = summarize_trades([
        simulate_trade(win_bars, shock, confirmation),
        simulate_trade(loss_bars, shock2, confirmation2),
    ])
    assert summary["trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["profit_factor"] > 0
    assert summary["avg_mae"] < 0
