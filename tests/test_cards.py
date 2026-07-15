from datetime import datetime, timezone

from quant_signal.notifier import cards as cards_module
from quant_signal.notifier.cards import (
    premarket_cards,
    report_card,
    signal_card,
    us_briefing_card,
)
from quant_signal.strategies.base import Direction, Signal

TS = datetime(2026, 1, 2, tzinfo=timezone.utc)
INTL = {"7709.HK": "HKD", "000660.KS": "KRW"}
SELL_RELIABILITY_NOTE = "⚠️ SELL 信号历史胜率偏低（回测 32–42%，牛市窗口），仅供参考"


def _sig(ticker, direction, strategy_id, price=10.0, reason="r", rank=None, momentum=None):  # type: ignore[no-untyped-def]
    extra: dict[str, object] = {}
    if rank is not None:
        extra["rank"] = rank
    if momentum is not None:
        extra["momentum_60d"] = momentum
    return Signal(
        ticker=ticker, direction=direction, price=price, reason=reason,
        strategy_id=strategy_id, ts=TS, extra=extra or None,
    )


def _card_by(cards, needle):  # type: ignore[no-untyped-def]
    return next(c for c in cards if needle in c.title)


def test_signal_card_labels_sell_once_after_delayed_warning() -> None:
    signal = _sig("MU", Direction.SELL, "momentum_rotation")

    card = signal_card(signal, delayed=True)

    assert card.body_md.count(SELL_RELIABILITY_NOTE) == 1
    assert "⚠️ 数据延迟约15分钟，仅供观察" in card.body_md
    assert card.body_md.endswith(SELL_RELIABILITY_NOTE)
    assert card.sections == ()


def test_signal_card_does_not_label_buy() -> None:
    card = signal_card(_sig("MU", Direction.BUY, "momentum_rotation"))

    assert SELL_RELIABILITY_NOTE not in card.body_md


def test_premarket_market_card_appends_one_note_after_multiple_sell_rows() -> None:
    signals = [
        _sig("MU", Direction.SELL, "momentum_rotation"),
        _sig("GLD", Direction.SELL, "macd_cross"),
    ]

    card = _card_by(premarket_cards(signals, INTL, {"MU": None, "GLD": None}), "美股组")

    assert card.body_md.count(SELL_RELIABILITY_NOTE) == 1
    assert card.body_md.index("| MU | SELL |") < card.body_md.index(SELL_RELIABILITY_NOTE)
    assert card.body_md.index("| GLD | SELL |") < card.body_md.index(SELL_RELIABILITY_NOTE)
    assert card.body_md.endswith(SELL_RELIABILITY_NOTE)
    assert card.sections == ()


def test_premarket_market_card_does_not_label_buy_only_rows() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation")]

    card = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")

    assert SELL_RELIABILITY_NOTE not in card.body_md


def test_premarket_confluence_sell_card_appends_one_note() -> None:
    signals = [
        _sig("MU", Direction.SELL, "momentum_rotation"),
        _sig("MU", Direction.SELL, "macd_cross"),
    ]

    card = _card_by(premarket_cards(signals, INTL, {"MU": None}), "【重要】")

    assert card.body_md.count(SELL_RELIABILITY_NOTE) == 1
    assert card.body_md.index("| MU | SELL |") < card.body_md.index(SELL_RELIABILITY_NOTE)
    assert card.body_md.endswith(SELL_RELIABILITY_NOTE)


def test_generic_report_card_does_not_infer_sell_reliability_note() -> None:
    card = report_card("📋 无关日报", "| 标的 | 方向 |\n|---|---|\n| MU | SELL |")

    assert SELL_RELIABILITY_NOTE not in card.body_md


def test_splits_markets_us_first_and_skips_empty() -> None:
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("7709.HK", Direction.BUY, "momentum_rotation", rank=2),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None, "7709.HK": None})
    titles = [c.title for c in cards]
    assert titles == ["📋 盘前早报 · 美股组", "📋 盘前早报 · 港股组"]


def test_confluence_card_first_when_two_strategies_same_direction() -> None:
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("MU", Direction.BUY, "macd_cross"),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert "【重要】" in cards[0].title
    assert "MU" in cards[0].body_md and "动量轮动+MACD" in cards[0].body_md
    us = _card_by(cards, "美股组")
    assert "【动量轮动】" in us.body_md and "【MACD】" in us.body_md


def test_no_confluence_card_when_single_strategy() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", rank=1)]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert all("【重要】" not in c.title for c in cards)


def test_opposite_directions_are_not_confluence() -> None:
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("MU", Direction.SELL, "macd_cross"),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert all("【重要】" not in c.title for c in cards)


def test_market_card_sections_by_strategy_and_skip_empty() -> None:
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("GLD", Direction.BUY, "macd_cross"),
    ]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None, "GLD": None}), "美股组")
    assert "【动量轮动】" in us.body_md and "【MACD】" in us.body_md
    assert "【RSI回归】" not in us.body_md and "【布林带】" not in us.body_md


def test_momentum_sell_routed_to_ticker_market() -> None:
    signals = [_sig("000660.KS", Direction.SELL, "momentum_rotation")]
    cards = premarket_cards(signals, INTL, {"000660.KS": None})
    assert [c.title for c in cards] == ["📋 盘前早报 · 韩股组"]
    assert "SELL" in cards[0].body_md


def test_live_price_missing_shows_dash() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "| 100.00 | - |" in us.body_md


def test_live_price_present_formatted() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    us = _card_by(premarket_cards(signals, INTL, {"MU": 123.456}), "美股组")
    assert "123.46" in us.body_md


def test_take_profit_and_stop_loss_shown() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    signals[0].extra.update({"take_profit": 117.0, "stop_loss": 92.0})  # type: ignore[union-attr]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "止盈目标" in us.body_md and "止损价" in us.body_md
    assert "117.00" in us.body_md and "92.00" in us.body_md


def test_stop_loss_breach_marked_when_above_price() -> None:
    signals = [_sig("DRAM", Direction.BUY, "momentum_rotation", price=60.74, rank=1)]
    signals[0].extra["stop_loss"] = 62.73  # type: ignore[index]  # 止损在现价上方=已破位
    us = _card_by(premarket_cards(signals, INTL, {"DRAM": None}), "美股组")
    assert "62.73 ⚠破位" in us.body_md


def test_target_buy_shown_with_overheat_mark() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    signals[0].extra.update({"target_buy": 93.5, "overheat": True})  # type: ignore[union-attr]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "目标买入价" in us.body_md
    assert "93.50" in us.body_md and "⚠过热" in us.body_md


def test_earnings_surprise_context_in_reason() -> None:
    up = [_sig("MU", Direction.BUY, "momentum_rotation", price=10.0, rank=1)]
    up[0].extra["earnings_surprise"] = 12.3  # type: ignore[index]
    body = _card_by(premarket_cards(up, INTL, {"MU": None}), "美股组").body_md
    assert "财报超预期+12%" in body
    down = [_sig("MU", Direction.BUY, "momentum_rotation", price=10.0, rank=1)]
    down[0].extra["earnings_surprise"] = -8.0  # type: ignore[index]
    body = _card_by(premarket_cards(down, INTL, {"MU": None}), "美股组").body_md
    assert "⚠财报不及预期-8%" in body


def test_quality_flag_marked_in_reason() -> None:
    signals = [_sig("BADCO", Direction.BUY, "momentum_rotation", price=10.0, rank=1)]
    signals[0].extra["quality_flag"] = "ROE为负/高负债"  # type: ignore[index]
    us = _card_by(premarket_cards(signals, INTL, {"BADCO": None}), "美股组")
    assert "⚠质量差(ROE为负/高负债)" in us.body_md


def test_earnings_soon_marked_in_reason() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    signals[0].extra["earnings_in_days"] = 3  # type: ignore[index]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "⚠3天后财报" in us.body_md


def test_sell_row_shows_holding_return() -> None:
    signals = [_sig("MU", Direction.SELL, "momentum_rotation", price=120.0, momentum=0.0)]
    signals[0].extra["holding_return"] = 0.234  # type: ignore[index]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "持有期 +23.4%" in us.body_md


def test_exit_prices_dash_when_absent() -> None:
    signals = [_sig("GLD", Direction.BUY, "macd_cross")]
    us = _card_by(premarket_cards(signals, INTL, {"GLD": None}), "美股组")
    assert "止盈目标" in us.body_md and "止损价" in us.body_md  # 列头始终在
    # GLD(MACD)无止盈/止损，两格显示 -


def test_momentum_section_sorted_by_momentum_desc() -> None:
    """同一动量小节里 ETF/个股混排时按动量降序，不因组内名次而交错。"""
    signals = [
        _sig("AMD", Direction.BUY, "momentum_rotation", momentum=0.30),
        _sig("MU", Direction.BUY, "momentum_rotation", momentum=0.90),
        _sig("SMH", Direction.BUY, "momentum_rotation", momentum=0.50),
    ]
    us = _card_by(premarket_cards(signals, INTL, {}), "美股组")
    assert us.body_md.index("MU") < us.body_md.index("SMH") < us.body_md.index("AMD")


def test_momentum_ranking_card_shows_top_bottom_and_risk_markers() -> None:
    ranking = [
        ("TOP1", 0.50, 150.0),
        ("TOP2", 0.40, 140.0),
        ("TOP3", 0.30, 130.0),
        ("TOP4", 0.20, 120.0),
        ("TOP5", 0.10, 110.0),
        ("MID", 0.05, 105.0),
        ("LOW1", -0.10, 90.0),
        ("LOW2", -0.20, 80.0),
        ("LOW3", -0.30, 70.0),
    ]

    card = cards_module.momentum_ranking_card(
        ranking,
        held={"TOP2", "LOW2"},
        trend_flat={"MID"},
        insufficient={"TOP5"},
    )

    assert card.title == "📊 动量全池榜单"
    assert "Top 5 买入候选" in card.body_md
    assert "Bottom 3 卖出警示" in card.body_md
    assert "TOP2 ⚠持仓" in card.body_md
    assert "LOW2 ⚠持仓" in card.body_md
    assert "MID ⚠趋势FLAT" in card.body_md
    assert "TOP5 ⚠数据不足" in card.body_md
    assert "LOW1" in card.body_md and "LOW3" in card.body_md


def test_execution_plan_card_shows_paper_label_and_block_reasons() -> None:
    from datetime import date, datetime, timezone

    from quant_signal.execution import ExecutionPlan, PlanState
    from quant_signal.notifier.cards import execution_plan_card

    now = datetime(2026, 7, 10, 12, 15, tzinfo=timezone.utc)
    plan = ExecutionPlan(
        plan_id="p1",
        plan_version=1,
        plan_date=date(2026, 7, 10),
        ticker="AAPL",
        currency="USD",
        source_strategies=("index_scan",),
        memberships=("sp500",),
        score=0.9,
        entry_low=100.0,
        entry_high=102.0,
        limit_price=102.0,
        stop_loss=95.0,
        take_profit=115.0,
        target_weight=None,
        gap_qty=None,
        risk_qty=71,
        cash_qty=294,
        cap_qty=117,
        suggested_qty=71,
        suggested_notional=7242.0,
        valid_from=now,
        expires_at=datetime(2026, 7, 10, 19, 45, tzinfo=timezone.utc),
        quote_at=now,
        account_at=now,
        state=PlanState.CANDIDATE,
        block_reason=None,
        rule_version="exec-v1",
    )
    blocked = ExecutionPlan(
        **{
            **{f: getattr(plan, f) for f in plan.__dataclass_fields__},
            "plan_id": "p2",
            "ticker": "MSFT",
            "suggested_qty": None,
            "suggested_notional": None,
            "state": PlanState.BLOCKED,
            "block_reason": "STALE_ACCOUNT",
        }
    )

    card = execution_plan_card(None, [plan, blocked], now)

    assert "PAPER" in card.title or "PAPER" in card.body_md
    body = card.body_md
    assert "AAPL" in body and "MSFT" in body
    assert "102" in body and "95" in body and "115" in body
    assert "71" in body and "$7,242.00" in body
    assert "STALE_ACCOUNT" in body
    assert "账户数据不足" in body  # account=None 时的提示


def test_plan_event_card_is_account_source_neutral() -> None:
    from datetime import date, datetime, timezone

    from quant_signal.execution import ExecutionPlan, PlanState
    from quant_signal.notifier.cards import plan_event_card

    now = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)
    plan = ExecutionPlan(
        plan_id="p1",
        plan_version=1,
        plan_date=date(2026, 7, 10),
        ticker="AAPL",
        currency="USD",
        source_strategies=("index_scan",),
        memberships=("sp500",),
        score=0.9,
        entry_low=100.0,
        entry_high=102.0,
        limit_price=102.0,
        stop_loss=95.0,
        take_profit=115.0,
        target_weight=None,
        gap_qty=None,
        risk_qty=71,
        cash_qty=294,
        cap_qty=117,
        suggested_qty=71,
        suggested_notional=7242.0,
        valid_from=now,
        expires_at=datetime(2026, 7, 10, 19, 45, tzinfo=timezone.utc),
        quote_at=now,
        account_at=now,
        state=PlanState.ACTIONABLE,
        block_reason=None,
        rule_version="exec-v1",
    )

    card = plan_event_card(plan, "ACTIONABLE", price=101.0, at=now)

    assert "PAPER" not in card.title and "PAPER" not in card.body_md
    assert "观察模式" in card.body_md
    assert "ACTIONABLE" in card.title or "ACTIONABLE" in card.body_md
    assert "102" in card.body_md and "95" in card.body_md
    assert "71" in card.body_md


def test_us_briefing_card_is_compact_and_action_oriented() -> None:
    card = us_briefing_card(
        report_kind="US_CLOSE",
        as_of="2026-07-14",
        regime={
            "regime": "PULLBACK",
            "breadth_above_50d": 0.58,
            "realized_volatility": 0.24,
            "reasons": ["SHORT_TERM_DETERIORATION"],
        },
        candidates=[
            {
                "ticker": "AAPL",
                "lane": "TREND_PULLBACK",
                "entry_low": 205.0,
                "entry_high": 208.0,
                "invalidation_price": 198.0,
                "target_price": 228.0,
                "suggested_qty": 3,
                "suggested_notional": 624.0,
            }
        ],
        discipline=[
            {
                "ticker": "MU",
                "status": "TAKE_PROFIT_DUE",
                "current_price": "116.00",
                "cost_basis": "100.00",
                "cost_quality": "EXACT",
                "incremental_sell_qty": "25",
                "incremental_sell_fraction": "0.25",
                "cumulative_sell_fraction": "0.25",
                "protection_price": "102.00",
            }
        ],
        portfolio_risk={
            "total_effective_weight": "0.82",
            "leveraged_effective_weight": "0.25",
            "warnings": ["CLUSTER_EFFECTIVE_EXPOSURE_HIGH"],
        },
        observations=[
            {"ticker": "SKHY", "reason": "INSUFFICIENT_HISTORY", "history_days": 4}
        ],
        data_quality=["纳指100覆盖率 99%"],
    )

    assert "回调" in card.body_md
    assert "AAPL" in card.body_md
    assert "建议 3 股 / $624.00" in card.body_md
    assert "卖出 25 股（累计 25%）" in card.body_md
    assert "SKHY：历史仅 4 个交易日" in card.body_md
    assert "|---" not in card.body_md
    assert len(card.sections) <= 7


def test_us_briefing_card_summarizes_large_observation_pool() -> None:
    observations = [
        {"ticker": f"HOT{index:02d}", "reason": "OVERHEATED", "history_days": 220}
        for index in range(40)
    ] + [
        {"ticker": f"ILL{index:02d}", "reason": "LIQUIDITY_FILTER", "history_days": 220}
        for index in range(25)
    ] + [
        {"ticker": "SKHY", "reason": "INSUFFICIENT_HISTORY", "history_days": 4}
    ]

    card = us_briefing_card(
        report_kind="US_CLOSE",
        as_of="2026-07-14",
        regime={"regime": "RANGE", "reasons": ["NO_DIRECTIONAL_EDGE"]},
        candidates=[],
        discipline=[],
        portfolio_risk={},
        observations=observations,
        data_quality=["纳指100覆盖率 99%"],
    )

    assert "过热不追 40" in card.body_md
    assert "流动性过滤 25" in card.body_md
    assert "SKHY：历史仅 4 个交易日" in card.body_md
    assert "HOT00" not in card.body_md and "ILL00" not in card.body_md
    assert len(card.body_md.splitlines()) < 25


def test_us_briefing_card_never_invents_qty_for_partial_position() -> None:
    card = us_briefing_card(
        report_kind="ASIA_CONFIRM",
        as_of="2026-07-14",
        regime={"regime": "RANGE", "reasons": ["NO_DIRECTIONAL_EDGE"]},
        candidates=[],
        discipline=[
            {
                "ticker": "RAM",
                "status": "TAKE_PROFIT_DUE",
                "cost_basis": "50.00",
                "cost_quality": "ESTIMATED",
                "incremental_sell_qty": None,
                "incremental_sell_fraction": "0.25",
                "cumulative_sell_fraction": "0.25",
                "protection_price": "51.00",
            }
        ],
        portfolio_risk={},
        observations=[],
        data_quality=["持仓截图不完整"],
    )

    assert "卖出 25%（股数不可用）" in card.body_md
    assert "成本估算" in card.body_md


def test_us_briefing_card_keeps_unconfirmed_profit_action_visible() -> None:
    card = us_briefing_card(
        report_kind="US_CLOSE",
        as_of="2026-07-15",
        regime={"regime": "TREND", "reasons": ["TREND_AND_BREADTH_HEALTHY"]},
        candidates=[],
        discipline=[
            {
                "ticker": "MU",
                "status": "TAKE_PROFIT_DUE",
                "cost_basis": "100.00",
                "cost_quality": "EXACT",
                "incremental_sell_qty": "0",
                "incremental_sell_fraction": "0",
                "pending_sell_fraction": "0.25",
                "cumulative_sell_fraction": "0.25",
                "protection_price": "102.00",
            }
        ],
        portfolio_risk={},
        observations=[],
        data_quality=[],
    )

    assert "止盈仍待执行（累计应减 25%）" in card.body_md
    assert "继续持有/观察" not in card.body_md
