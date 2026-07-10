from datetime import date, datetime, timedelta, timezone

from quant_signal.config import ExecutionPlanSettings
from quant_signal.execution import PlanCandidate, PlanObservation, build_plan
from quant_signal.execution_replay import replay_plans

CONFIG = ExecutionPlanSettings(enabled=True)
NOW = datetime(2026, 7, 10, 12, 15, tzinfo=timezone.utc)
SESSION_START = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)  # 10:00 ET


def make_plan(ticker: str, entry_low: float = 100.0, entry_high: float = 102.0):
    candidate = PlanCandidate(
        ticker=ticker,
        plan_date=date(2026, 7, 10),
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=95.0,
        take_profit=115.0,
        target_weight=0.1,
        score=0.9,
        source_strategies=("index_scan",),
        memberships=("sp500",),
        quote_at=NOW,
    )
    from decimal import Decimal

    from quant_signal.account import AccountSnapshot

    account = AccountSnapshot(
        account_id="paper-1",
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        currency="USD",
        retrieved_at=NOW,
    )
    return build_plan(candidate, account, (), (), CONFIG, NOW)


def obs_seq(prices_and_bars: list[tuple[float, float | None]]) -> list[PlanObservation]:
    """(price, complete_bar_close) 序列 -> 5 分钟递进观测。"""
    output = []
    for offset, (price, bar_close) in enumerate(prices_and_bars):
        output.append(
            PlanObservation(
                at=SESSION_START + timedelta(minutes=5 * offset),
                price=price,
                bar_close=bar_close,
                bar_low=(bar_close * 0.999 if bar_close is not None else None),
                bar_complete=bar_close is not None,
            )
        )
    return output


def test_replay_separates_old_target_hits_from_confirmed_actionable() -> None:
    # 旧口径: live <= target_buy(=entry_low) * 1.002 即提醒
    # WICK: 瞬间触及目标价但无完整bar收在买区 -> 旧口径命中, 新口径不 ACTIONABLE
    # SOLID: 触及后有完整bar收在买区 -> 两个口径都命中
    plans = [make_plan("WICK"), make_plan("SOLID")]
    observations = {
        "WICK": obs_seq([(105.0, None), (100.1, None), (104.0, 104.0), (105.0, 105.0)]),
        "SOLID": obs_seq([(105.0, None), (100.1, 100.1), (101.0, 101.0)]),
    }
    daily_closes = {
        "WICK": [104.0, 104.5, 105.0],
        "SOLID": [101.0, 103.0, 106.0],
    }

    result = replay_plans(plans, observations, daily_closes, CONFIG)

    assert result.old_target_hits == 2
    assert result.new_actionable == 1
    assert result.entry_rate == 0.5
    assert result.survivorship_biased is True


def test_replay_reports_forward_returns_and_max_adverse_excursion() -> None:
    plans = [make_plan("SOLID")]
    observations = {
        "SOLID": obs_seq([(105.0, None), (101.5, None), (101.0, 101.0)])
    }
    # 入场价 = 确认bar收盘 101.0; 后续日收盘 99(回撤) -> 106
    daily_closes = {"SOLID": [101.0, 99.0, 103.0, 104.0, 105.0, 106.0]}

    result = replay_plans(plans, observations, daily_closes, CONFIG)

    assert result.new_actionable == 1
    entry = result.entries[0]
    assert entry.ticker == "SOLID"
    assert entry.entry_price == 101.0
    assert entry.fwd_1d == (99.0 / 101.0 - 1.0)
    assert entry.fwd_5d == (106.0 / 101.0 - 1.0)
    assert entry.fwd_20d is None  # 数据不足 20 天, 不得虚报
    assert entry.max_adverse_excursion == (99.0 / 101.0 - 1.0)


def test_replay_with_no_observations_counts_nothing() -> None:
    plans = [make_plan("EMPTY")]
    result = replay_plans(plans, {}, {}, CONFIG)
    assert result.old_target_hits == 0
    assert result.new_actionable == 0
    assert result.entry_rate == 0.0
    assert result.entries == ()
