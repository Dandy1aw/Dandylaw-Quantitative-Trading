import pandas as pd
from decimal import Decimal

from quant_signal.performance import (
    Trade,
    broker_fill_performance,
    build_horizon_trades,
    build_round_trips,
    performance_card,
    strategy_summary,
)


def _row(sid: str, ticker: str, direction: str, price: float, at: str) -> dict[str, object]:
    return {"strategy_id": sid, "ticker": ticker, "direction": direction,
            "price": price, "pushed_at": at}


def test_round_trips_pair_buy_then_sell() -> None:
    rows = [
        _row("momentum_rotation", "MU", "buy", 100.0, "2026-06-01"),
        _row("momentum_rotation", "MU", "buy", 105.0, "2026-06-05"),   # 持仓中重复BUY忽略
        _row("momentum_rotation", "MU", "sell", 120.0, "2026-06-20"),
        _row("momentum_rotation", "MU", "sell", 119.0, "2026-06-21"),  # 空仓SELL忽略
        _row("macd_cross", "GLD", "buy", 200.0, "2026-06-10"),         # 未平仓
    ]
    trades = build_round_trips(rows)
    closed = [t for t in trades if t.closed]
    assert len(closed) == 1 and closed[0].entry_price == 100.0 and closed[0].exit_price == 120.0
    assert [t.ticker for t in trades if not t.closed] == ["GLD"]


def test_round_trips_exclude_alert_strategies() -> None:
    rows = [_row("price_deviation", "MU", "buy", 100.0, "2026-06-01")]
    assert build_round_trips(rows) == []


def _daily_bars(n: int = 25) -> pd.DataFrame:
    idx = pd.bdate_range("2026-06-02", periods=n, tz="UTC")
    return pd.DataFrame(
        {
            "open": range(100, 100 + n),
            "high": range(101, 101 + n),
            "low": range(99, 99 + n),
            "close": range(100, 100 + n),
            "volume": 1_000_000,
        },
        index=pd.MultiIndex.from_product([["MU"], idx], names=["ticker", "ts"]),
    )


def test_market_scan_horizon_enters_next_open_and_exits_day_20_close() -> None:
    rows = [_row("market_scan", "MU", "buy", 999.0, "2026-06-01T11:00:00+00:00")]

    trades = build_horizon_trades(rows, _daily_bars(), horizon_days=20)

    assert len(trades) == 1
    assert trades[0].entry_price == 100.0
    assert trades[0].exit_price == 119.0
    assert trades[0].closed


def test_market_scan_horizon_is_open_before_maturity() -> None:
    rows = [_row("market_scan", "MU", "buy", 999.0, "2026-06-01T11:00:00+00:00")]

    trades = build_horizon_trades(rows, _daily_bars(10), horizon_days=20)

    assert len(trades) == 1 and not trades[0].closed
    assert trades[0].entry_price == 100.0


def test_market_scan_horizon_skips_signal_without_next_open() -> None:
    rows = [_row("market_scan", "MU", "buy", 999.0, "2026-09-01T11:00:00+00:00")]
    assert build_horizon_trades(rows, _daily_bars(), horizon_days=20) == []


def test_round_trips_leave_market_scan_to_horizon_builder() -> None:
    rows = [_row("market_scan", "MU", "buy", 100.0, "2026-06-01")]
    assert build_round_trips(rows) == []


def test_strategy_summary_win_rate_and_open_mark() -> None:
    trades = [
        Trade("momentum_rotation", "MU", 100.0, "a", 120.0, "b"),   # +20%
        Trade("momentum_rotation", "AMD", 100.0, "a", 90.0, "b"),   # -10%
        Trade("momentum_rotation", "TSM", 100.0, "a"),              # open, mark 110
        Trade("momentum_rotation", "GLW", 100.0, "a"),              # open, 无mark不计
    ]
    s = strategy_summary(trades, marks={"TSM": 110.0})["momentum_rotation"]
    assert s["closed"] == 2 and s["win_rate"] == 0.5
    assert abs(s["avg_ret"] - 0.05) < 1e-9
    assert s["open"] == 2 and abs(s["open_avg_ret"] - 0.10) < 1e-9


def test_performance_card_renders_table_and_empty() -> None:
    summary = {"momentum_rotation": {"closed": 2.0, "win_rate": 0.5, "avg_ret": 0.05,
                                     "open": 1.0, "open_avg_ret": 0.1}}
    card = performance_card(summary, window_days=90, benchmark_note="同期 SPY +3.0%")
    assert "动量轮动" in card.body_md and "50%" in card.body_md and "SPY" in card.body_md
    empty = performance_card({}, window_days=90)
    assert "暂无" in empty.body_md


def test_broker_fill_performance_uses_fifo_and_exposes_missing_basis() -> None:
    rows = [
        {"source": "alpaca_paper", "currency": "USD", "symbol": "MU", "side": "buy",
         "filled_qty": "2", "filled_avg_price": "100"},
        {"source": "alpaca_paper", "currency": "USD", "symbol": "MU", "side": "buy",
         "filled_qty": "1", "filled_avg_price": "110"},
        {"source": "alpaca_paper", "currency": "USD", "symbol": "MU", "side": "sell",
         "filled_qty": "4", "filled_avg_price": "120"},
    ]

    summary = broker_fill_performance(rows)[0]

    assert summary.realized_pnl == Decimal("50")
    assert summary.matched_cost == Decimal("310")
    assert summary.matched_quantity == Decimal("3")
    assert summary.unmatched_sell_quantity == Decimal("1")
    assert summary.is_live_source is False


def test_performance_card_labels_paper_fills_without_calling_them_live() -> None:
    fills = broker_fill_performance(
        [{"source": "alpaca_paper", "currency": "USD", "symbol": "MU", "side": "buy",
          "filled_qty": "1", "filled_avg_price": "100"}]
    )

    card = performance_card({}, 90, fill_performance=fills)

    assert "模拟/观察源" in card.body_md
    assert "真实成交" not in card.body_md
