from datetime import UTC, date, datetime, timezone

from quant_signal.fear_dca import (
    ETFMetrics,
    FearInterpretation,
    FearMetrics,
    RecommendationDecision,
)

from quant_signal.notifier import cards as cards_module
from quant_signal.notifier.cards import (
    extreme_movers_close_card,
    extreme_movers_premarket_card,
    extreme_mover_sectors_card,
    fear_dca_card,
    fear_dca_incomplete_card,
    fear_dca_rules_card,
    premarket_cards,
    report_card,
    signal_card,
    us_briefing_card,
)
from quant_signal.extreme_movers import (
    Eligibility,
    ExtremeMoverEvent,
    MoverDirection,
    rank_movers,
    rank_sectors,
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


def _mover(ticker: str, direction: MoverDirection, value: str) -> ExtremeMoverEvent:
    from datetime import date
    from decimal import Decimal

    return ExtremeMoverEvent(
        session=date(2026, 8, 7), ticker=ticker, direction=direction,
        daily_return=Decimal(value), close=Decimal("20"),
        avg_dollar_volume_20d=Decimal("30000000"),
        sector="Information Technology", industry="Software", quote_type="EQUITY",
        eligibility=Eligibility.ELIGIBLE,
    )


def test_extreme_mover_close_card_has_separate_boards_and_coverage() -> None:
    card = extreme_movers_close_card(
        [_mover("UP", MoverDirection.UP, "0.12"), _mover("DN", MoverDirection.DOWN, "-0.11")],
        universe_count=100,
        covered_count=98,
    )

    assert "上涨 ≥10%" in card.body_md
    assert "下跌 ≤-10%" in card.body_md
    assert "覆盖 98/100" in card.body_md
    assert "推荐" not in card.body_md


def test_extreme_mover_premarket_card_shows_sector_top5() -> None:
    events = [_mover("A", MoverDirection.UP, "0.12"), _mover("B", MoverDirection.UP, "0.11")]
    card = extreme_movers_premarket_card(
        session=events[0].session,
        window_sessions=60,
        movers=rank_movers(events, window_sessions=60),
        sectors=rank_sectors(events, window_sessions=60),
    )

    assert "盘前" in card.title
    assert "板块 Top5" in card.body_md
    assert "累计入榜 2 天" in card.body_md
    assert "市场宽度" in card.body_md


def test_extreme_mover_sector_card_can_filter_one_sector() -> None:
    events = [_mover("A", MoverDirection.UP, "0.12")]
    card = extreme_mover_sectors_card(
        session=events[0].session,
        window_sessions=60,
        sectors=rank_sectors(events, window_sessions=60),
        sector_filter="Information Technology",
    )

    assert "Information Technology" in card.body_md
    assert "上涨个股" not in card.body_md
    assert "筛选" in card.body_md


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


def test_us_briefing_card_groups_large_caps_by_sector_with_company_rationale() -> None:
    candidates = [
        {
            "ticker": "MSFT",
            "company_name": "Microsoft",
            "gics_sector": "Information Technology",
            "candidate_group": "Technology",
            "industry": "Software - Infrastructure",
            "market_cap_usd": 3_500_000_000_000,
            "sector_strategy_rank": 1,
            "sector_market_cap_rank": 1,
            "profile_as_of": "2026-07-14",
            "lane": "TREND_CONTINUATION",
            "entry_low": 500.0,
            "entry_high": 505.0,
            "invalidation_price": 480.0,
            "target_price": 550.0,
            "suggested_qty": 1,
            "suggested_notional": 500.0,
        },
        {
            "ticker": "NVDA",
            "company_name": "NVIDIA",
            "gics_sector": "Information Technology",
            "candidate_group": "Semiconductors",
            "industry": "Semiconductors",
            "market_cap_usd": 2_300_000_000_000,
            "sector_strategy_rank": 1,
            "sector_market_cap_rank": 1,
            "profile_as_of": "2026-07-14",
            "lane": "TREND_PULLBACK",
            "entry_low": 220.0,
            "entry_high": 225.0,
            "invalidation_price": 210.0,
            "target_price": 250.0,
            "suggested_qty": 2,
            "suggested_notional": 440.0,
        },
    ]
    rationale = (
        "上涨逻辑：云业务需求支持增长。\n"
        "行业地位：行业策略第1，合格同行市值第1。\n"
        "壁垒：客户迁移成本较高。\n"
        "反证：云增速下降会令逻辑失效。"
    )
    card = us_briefing_card(
        report_kind="DAILY_ACTION",
        as_of="2026-07-14",
        regime={"regime": "TREND", "reasons": ["TREND_AND_BREADTH_HEALTHY"]},
        candidates=candidates,
        discipline=[],
        portfolio_risk={},
        observations=[],
        data_quality=["纳指100覆盖率 99%"],
        company_rationales={"MSFT": rationale},
    )

    assert "行业分组候选 · 市值门槛 ≥ 1000亿美元" in card.body_md
    assert "**科技**" in card.body_md
    assert "**半导体**" in card.body_md
    assert card.body_md.count("**Information Technology**") == 0
    assert "策略 #1 · 合格同行市值 #1 · 市值 3.50万亿美元" in card.body_md
    assert "画像 2026-07-14 · 新闻窗口近7日" in card.body_md
    assert "上涨逻辑：云业务需求支持增长" in card.body_md


def test_us_briefing_card_uses_chinese_labels_and_three_stage_targets() -> None:
    candidates = [
        {
            "ticker": "MSFT",
            "company_name": "Microsoft",
            "candidate_group": "Technology",
            "industry": "Computer Hardware",
            "market_cap_usd": 3_500_000_000_000,
            "lane": "TREND_CONTINUATION",
            "entry_low": 500.0,
            "entry_high": 505.0,
            "invalidation_price": 480.0,
            "target_price": 555.5,
            "profit_targets": [530.25, 555.5, 580.75],
            "recommended_target_stage": 2,
            "recent_buying_notional": 2_260_000_000,
            "buying_pressure_score": 0.58,
            "buying_pressure_label": "中等",
            "nearby_resistance": 570.0,
            "suggested_qty": 1,
            "suggested_notional": 505.0,
        },
        {
            "ticker": "SNDK",
            "company_name": "Sandisk Corporation",
            "candidate_group": "Semiconductors",
            "industry": "Semiconductors",
            "market_cap_usd": 236_000_000_000,
            "lane": "TREND_PULLBACK",
            "entry_low": 1600.0,
            "entry_high": 1621.0,
            "invalidation_price": 1199.0,
            "target_price": 1702.05,
            "profit_targets": [1702.05, 1783.1, 1864.15],
            "recommended_target_stage": 1,
            "recent_buying_notional": 800_000_000,
            "buying_pressure_score": 0.30,
            "buying_pressure_label": "偏弱",
            "suggested_qty": None,
            "suggested_notional": None,
            "block_reason": "STOP_TOO_WIDE",
        },
    ]

    card = us_briefing_card(
        report_kind="DAILY_ACTION",
        as_of="2026-07-22",
        regime={"regime": "TREND", "reasons": []},
        candidates=candidates,
        discipline=[],
        portfolio_risk={},
        observations=[],
        data_quality=[],
    )

    assert "**科技**" in card.body_md
    assert "**半导体**" in card.body_md
    assert "计算机硬件" in card.body_md
    assert "止盈1 530.25 · 止盈2 555.50 · 止盈3 580.75" in card.body_md
    assert "建议第2档" in card.body_md
    assert "近5日买盘资金估算 22.60亿美元 · 强度中等 58%" in card.body_md
    blocked_line = next(line for line in card.body_md.splitlines() if "SNDK ·" in line)
    assert "仅观察：止损距离过大" in blocked_line
    assert "买入" not in blocked_line
    assert "STOP_TOO_WIDE" not in card.body_md


def test_us_briefing_card_translates_live_block_reasons_and_industries() -> None:
    card = us_briefing_card(
        report_kind="DAILY_ACTION",
        as_of="2026-07-22",
        regime={"regime": "PULLBACK", "reasons": []},
        candidates=[
            {
                "ticker": "CSCO",
                "company_name": "Cisco Systems, Inc.",
                "candidate_group": "Technology",
                "industry": "Communication Equipment",
                "lane": "TREND_PULLBACK",
                "block_reason": "ZERO_QTY",
            },
            {
                "ticker": "APP",
                "company_name": "Applovin Corporation",
                "candidate_group": "Communication Services",
                "industry": "Advertising Agencies",
                "lane": "RANGE_REVERSION",
                "block_reason": "MAX_NEW_POSITIONS",
            },
        ],
        discipline=[],
        portfolio_risk={},
        observations=[],
        data_quality=[],
    )

    assert "通信设备" in card.body_md
    assert "广告代理" in card.body_md
    assert "仅观察：可用资金不足，建议股数为0" in card.body_md
    assert "仅观察：当日新开仓数量已达上限" in card.body_md
    assert "ZERO_QTY" not in card.body_md
    assert "MAX_NEW_POSITIONS" not in card.body_md


def test_us_briefing_cards_split_action_summary_from_sector_candidates() -> None:
    from quant_signal.notifier import cards as cards_module

    candidates = [
        {
            "ticker": "MSFT",
            "candidate_group": "Technology",
            "lane": "TREND_CONTINUATION",
            "entry_low": 500.0,
            "entry_high": 505.0,
            "invalidation_price": 480.0,
            "target_price": 530.25,
            "suggested_qty": 1,
            "suggested_notional": 505.0,
        },
        {
            "ticker": "NVDA",
            "candidate_group": "Semiconductors",
            "lane": "TREND_PULLBACK",
            "entry_low": 200.0,
            "entry_high": 205.0,
            "invalidation_price": 190.0,
            "target_price": 215.25,
            "suggested_qty": 2,
            "suggested_notional": 410.0,
        },
    ]
    rationales = {
        "MSFT": "上涨逻辑：云业务需求支持增长。",
        "NVDA": "上涨逻辑：加速计算需求支持增长。",
    }

    cards = cards_module.us_briefing_cards(
        report_kind="DAILY_ACTION",
        as_of="2026-07-22",
        regime={"regime": "TREND", "reasons": []},
        candidates=candidates,
        discipline=[],
        portfolio_risk={},
        observations=[],
        data_quality=[],
        company_rationales=rationales,
    )

    assert len(cards) == 3
    assert "今日美股行动简报" in cards[0].title
    assert "MSFT" not in cards[0].body_md
    assert "NVDA" not in cards[0].body_md
    assert "上涨逻辑" not in cards[0].body_md
    tech = next(card for card in cards[1:] if "科技" in card.title)
    chips = next(card for card in cards[1:] if "半导体" in card.title)
    assert "MSFT" in tech.body_md and "NVDA" not in tech.body_md
    assert "NVDA" in chips.body_md and "MSFT" not in chips.body_md


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


def _fear_metrics(
    close: float,
    one_day: float,
    ma20: float,
    ma60: float,
    interpretation: FearInterpretation,
) -> FearMetrics:
    return FearMetrics(
        close=close,
        one_session_return=one_day,
        ma20=ma20,
        ma60=ma60,
        deviation_from_ma20=close / ma20 - 1.0,
        deviation_from_ma60=close / ma60 - 1.0,
        interpretation=interpretation,
    )


def test_fear_dca_card_contains_full_decision_context_and_rules() -> None:
    card = fear_dca_card(
        target_session=date(2026, 8, 12),
        generated_at=datetime(2026, 8, 13, 9, 30, tzinfo=UTC),
        vix_metrics=_fear_metrics(
            35.0, 0.10, 30.0, 28.0, FearInterpretation.TREND_CONFIRMED
        ),
        vxn_metrics=_fear_metrics(
            42.0, -0.05, 40.0, 38.0, FearInterpretation.TREND_CONFIRMED
        ),
        spy_metrics=ETFMetrics(500.0, -0.01, -0.03, -0.05),
        qqqm_metrics=ETFMetrics(220.0, 0.005, -0.02, -0.07),
        spy_decision=RecommendationDecision(
            1.5, 0.5, 2.0, "VIX tier and SPY drawdown triggered."
        ),
        qqqm_decision=RecommendationDecision(
            1.5, 0.5, 2.0, "VXN tier and QQQM drawdown triggered."
        ),
        image_key="img_v2_test",
    )

    assert card.title == "恐慌指数定投观察｜08/12 收盘"
    assert card.image_key == "img_v2_test"
    assert "数据日：2026-08-12" in card.body_md
    assert "生成时间：2026-08-13 17:30 CST" in card.body_md
    assert "Yahoo Finance，复权日线，最近已完成美股交易日" in card.body_md
    assert "VIX 35.00｜1日 +10.00%" in card.body_md
    assert "MA20 30.00（偏离 +16.67%）｜MA60 28.00（偏离 +25.00%）" in card.body_md
    assert "解读：恐慌趋势确认" in card.body_md
    assert "SPY 500.00｜1日 -1.00%｜5日 -3.00%｜20日 -5.00%" in card.body_md
    assert "VXN 42.00｜1日 -5.00%" in card.body_md
    assert "QQQM 220.00｜1日 +0.50%｜5日 -2.00%｜20日 -7.00%" in card.body_md
    assert card.body_md.count("基础 1.5×｜回撤加成 0.5×｜最终 2×") == 2
    assert "原因：VIX tier and SPY drawdown triggered." in card.body_md
    assert "原因：VXN tier and QQQM drawdown triggered." in card.body_md
    assert "VIX基础：<25 0×；25–<30 1×；30–<40 1.5×；40–<50 2×；≥50 3×" in card.body_md
    assert "VXN基础：<35 0×；35–<40 1×；40–<50 1.5×；50–<60 2×；≥60 3×" in card.body_md
    assert "SPY 5日≤-3%或20日≤-5%" in card.body_md
    assert "QQQM 5日≤-4%或20日≤-7%" in card.body_md
    assert "最终封顶3×" in card.body_md
    assert "0×=不额外恐慌加仓，不影响原定投" in card.body_md
    assert "不提供具体金额、订单或投资建议" in card.body_md


def test_fear_dca_incomplete_card_pauses_without_stale_multipliers() -> None:
    card = fear_dca_incomplete_card(
        target_session=date(2026, 8, 12),
        error="^VXN 缺少目标交易日收盘价\nplease retry later",
    )

    assert card.title == "恐慌指数定投提醒｜数据不完整"
    assert "数据不完整，今日推荐暂停" in card.body_md
    assert "目标交易日：2026-08-12" in card.body_md
    assert "^VXN 缺少目标交易日收盘价 please retry later" in card.body_md
    assert "×" not in card.body_md
    assert "基础" not in card.body_md
    assert "最终" not in card.body_md


def test_fear_dca_rules_card_is_static_and_complete() -> None:
    first = fear_dca_rules_card()
    second = fear_dca_rules_card()

    assert first == second
    assert first.title == "恐慌指数定投规则"
    assert "VIX基础：<25 0×；25–<30 1×；30–<40 1.5×；40–<50 2×；≥50 3×" in first.body_md
    assert "VXN基础：<35 0×；35–<40 1×；40–<50 1.5×；50–<60 2×；≥60 3×" in first.body_md
    assert "SPY 5日≤-3%或20日≤-5%" in first.body_md
    assert "QQQM 5日≤-4%或20日≤-7%" in first.body_md
    assert "最终封顶3×" in first.body_md
    assert "0×=不额外恐慌加仓，不影响原定投" in first.body_md
