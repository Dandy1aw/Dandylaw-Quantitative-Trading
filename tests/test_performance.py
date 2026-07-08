from quant_signal.performance import Trade, build_round_trips, performance_card, strategy_summary


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
