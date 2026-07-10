from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from quant_signal.account import AccountSnapshot, AccountState, ObservedPosition
from quant_signal.execution import ExecutionPlan, PlanState
from quant_signal.notifier.cards import execution_plan_card

NOW = datetime(2026, 7, 10, 12, 15, tzinfo=timezone.utc)


def plan(ticker: str, *, state: PlanState = PlanState.CANDIDATE, reason: str | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=f"p-{ticker}", plan_version=1, plan_date=date(2026, 7, 10),
        ticker=ticker, currency="USD", source_strategies=("index_scan",),
        memberships=("sp500",), score=0.9, entry_low=307.26, entry_high=316.22,
        limit_price=316.22, stop_loss=290.49, take_profit=341.68,
        target_weight=None, gap_qty=None, risk_qty=1, cash_qty=9, cap_qty=2,
        suggested_qty=1 if state is not PlanState.BLOCKED else None,
        suggested_notional=316.22 if state is not PlanState.BLOCKED else None,
        valid_from=NOW,
        expires_at=datetime(2026, 7, 10, 19, 45, tzinfo=timezone.utc),
        quote_at=NOW, account_at=NOW, state=state, block_reason=reason,
        rule_version="exec-v1",
    )


def screenshot_state() -> AccountState:
    observed = tuple(
        ObservedPosition(
            symbol=symbol, qty=None, avg_entry_price=None, current_price=None,
            market_value=None, estimated_market_value=Decimal(value),
            pnl=Decimal(pnl), pnl_pct=Decimal(pnl_pct), weight_pct=Decimal(weight),
            precision="ESTIMATED",
        )
        for symbol, value, pnl, pnl_pct, weight in (
            ("DRAM", "887.34", "-511.13", "-36.54", "14.80"),
            ("MU", "991.06", "166.82", "20.23", "16.53"),
            ("RAM", "363.93", "45.13", "14.16", "6.07"),
            ("SMH", "1226.68", "11.52", "0.95", "20.46"),
            ("SNXX", "774.62", "158.73", "25.77", "12.92"),
        )
    )
    return AccountState(
        snapshot=AccountSnapshot(
            account_id="screenshot:abc", equity=Decimal("5995.52"),
            cash=Decimal("1751.13"), buying_power=Decimal("3474.15"),
            currency="USD", retrieved_at=NOW, source="screenshot",
            market_value=Decimal("4244.15"), capital_limit=Decimal("6000"),
            max_financing_ratio=Decimal("0.20"),
        ),
        positions=(), open_orders=(), recent_orders=(), observed_positions=observed,
        positions_partial=True, reported_position_count=6,
    )


def test_action_card_is_mobile_compact_and_uses_real_account() -> None:
    plans = [
        plan("AAPL"), plan("NTAP", state=PlanState.ARMED),
        plan("AMD", state=PlanState.BLOCKED, reason="CLUSTER_WEIGHT_EXCEEDED"),
        plan("000660.KS", state=PlanState.BLOCKED, reason="UNSUPPORTED_MARKET"),
    ]
    card = execution_plan_card(
        screenshot_state(), plans, NOW,
        ai_summary="主线：科技。\n最大风险：集中。\n今日倾向：等待。",
    )

    assert card.title.startswith("🧭 今日行动计划")
    assert "PAPER" not in card.title and "PAPER" not in card.body_md
    assert "|---" not in card.body_md
    assert "净值 $5,995.52" in card.body_md
    assert "资金上限 $6,000.00" in card.body_md
    assert "融资上限 $1,200.00" in card.body_md
    assert "总敞口≤$7,200.00" in card.body_md
    assert "AAPL · 未持仓新买候选" in card.body_md
    assert "最多 1 股" in card.body_md
    assert "主题集中度超限" in card.body_md
    assert "不支持的市场/币种" in card.body_md
    assert "持仓明细不完整" in card.body_md
    assert "AI简评" in card.body_md
    assert len(card.sections) >= 5


def test_action_card_caps_visible_candidates_and_ai_length() -> None:
    candidates = [plan(ticker) for ticker in ("AAPL", "NTAP", "TECH", "LLY", "STT")]
    card = execution_plan_card(screenshot_state(), candidates, NOW, ai_summary="观" * 500)
    assert all(ticker in card.body_md for ticker in ("AAPL", "NTAP", "TECH", "LLY", "STT"))
    assert "观" * 301 not in card.body_md
