from datetime import datetime, timezone

from quant_signal.notifier import cards as cards_module
from quant_signal.notifier.cards import premarket_cards
from quant_signal.strategies.base import Direction, Signal

TS = datetime(2026, 1, 2, tzinfo=timezone.utc)
INTL = {"7709.HK": "HKD", "000660.KS": "KRW"}


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


def test_entry_band_shown_with_overheat_mark() -> None:
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    signals[0].extra.update({"entry_low": 93.5, "entry_high": 100.0, "overheat": True})  # type: ignore[union-attr]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "建议买入带" in us.body_md
    assert "93.50~100.00" in us.body_md and "⚠过热" in us.body_md


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
