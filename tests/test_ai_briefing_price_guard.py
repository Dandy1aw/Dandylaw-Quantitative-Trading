from __future__ import annotations

from datetime import datetime, timezone

from quant_signal.ai_briefing import AIBriefingContext, build_ai_briefing_prompt
from quant_signal.pipelines import premarket
from quant_signal.strategies.base import Direction, Signal


def test_prompt_makes_structured_signals_authoritative_for_prices() -> None:
    context = AIBriefingContext(
        as_of="2026-07-10T08:00:00+08:00",
        signals=[
            {
                "ticker": "MU",
                "direction": "BUY",
                "price": 991.12,
                "currency": "USD",
                "price_source": "structured_signal",
                "target_buy": 991.12,
                "take_profit": 1287.85,
                "stop_loss": 949.10,
            }
        ],
        analysis_cards=[
            {
                "title": "盘前早报",
                "body": "正文里可能还有动量、排名、成交额等数字，但不能从这里推导交易价格。",
            }
        ],
    )

    prompt = build_ai_briefing_prompt(context, max_chars=3000)

    assert "signals 是交易价位的唯一权威来源" in prompt
    assert "不要从 analysis_cards 正文推导、放大、缩小或换算价格" in prompt
    assert "必须保留并标注 currency" in prompt
    assert '"currency": "USD"' in prompt
    assert '"price_source": "structured_signal"' in prompt


def test_signal_context_attaches_currency_and_price_source() -> None:
    signal = Signal(
        ticker="000660.KS",
        direction=Direction.BUY,
        price=2180000.0,
        reason="momentum",
        strategy_id="momentum_rotation",
        ts=datetime(2026, 7, 10, tzinfo=timezone.utc),
        extra={
            "target_buy": 2180000.0,
            "take_profit": 2788483.86,
            "stop_loss": 2179357.14,
        },
    )

    rows = premarket._signal_context([signal], {"000660.KS": "KRW"})

    assert rows == [
        {
            "ticker": "000660.KS",
            "direction": "BUY",
            "price": 2180000.0,
            "currency": "KRW",
            "price_source": "structured_signal",
            "strategy": "momentum_rotation",
            "reason": "momentum",
            "target_buy": 2180000.0,
            "take_profit": 2788483.86,
            "stop_loss": 2179357.14,
        }
    ]
