from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import math

import pytest

from quant_signal.ledger import SignalLedger
from quant_signal.strategies.base import Direction, Signal, dedup_key

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def sig(
    ticker: str = "SPY",
    ts: datetime = NOW,
    strategy_id: str = "momentum_rotation",
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=Direction.BUY,
        price=100.0,
        reason="r",
        strategy_id=strategy_id,
        ts=ts,
        suggested_weight=0.33,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> SignalLedger:
    return SignalLedger(tmp_path / "signals.db")


def test_insert_and_query_day(ledger: SignalLedger) -> None:
    ledger.insert(sig(), pushed=True)
    ledger.insert(sig("QQQ"), pushed=False)
    rows = ledger.signals_on(date(2026, 7, 6))
    assert len(rows) == 2
    assert {r["ticker"] for r in rows} == {"SPY", "QQQ"}
    assert [r["pushed"] for r in sorted(rows, key=lambda r: str(r["ticker"]))] == [0, 1]


def test_ledger_usable_from_another_thread(ledger: SignalLedger) -> None:
    """调度器在后台线程池跑 job，ledger 的 sqlite 连接必须能跨线程使用
    (check_same_thread=False)，否则 rotation/premarket/watch_deviation 全崩。"""
    import threading

    errors: list[Exception] = []

    def worker() -> None:
        try:
            ledger.insert(sig("QQQ"), pushed=True, now=NOW)
            ledger.set_holdings("momentum_rotation", ["QQQ"])
            assert ledger.get_holdings("momentum_rotation") == ["QQQ"]
            assert ledger.pushed_count_since(NOW - timedelta(hours=1)) >= 1
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not errors, f"跨线程使用 ledger 失败: {errors}"


def test_last_push_by_key_only_pushed(ledger: SignalLedger) -> None:
    s = sig()
    ledger.insert(s, pushed=False, now=NOW)
    assert ledger.last_push_by_key(NOW - timedelta(hours=4)) == {}
    ledger.insert(s, pushed=True, now=NOW)
    got = ledger.last_push_by_key(NOW - timedelta(hours=4))
    assert got == {dedup_key(s): NOW}


def test_pushed_count_since(ledger: SignalLedger) -> None:
    ledger.insert(sig("A"), pushed=True, now=NOW)
    ledger.insert(sig("B"), pushed=True, now=NOW)
    ledger.insert(sig("C"), pushed=False, now=NOW)
    assert ledger.pushed_count_since(NOW - timedelta(hours=1)) == 2


def test_pushed_count_since_can_filter_strategy_channel(ledger: SignalLedger) -> None:
    ledger.insert(sig("A", strategy_id="momentum_rotation"), pushed=True, now=NOW)
    ledger.insert(sig("B", strategy_id="breakout_20d"), pushed=True, now=NOW)
    ledger.insert(sig("C", strategy_id="price_deviation"), pushed=True, now=NOW)

    assert ledger.pushed_count_since(
        NOW - timedelta(hours=1), {"momentum_rotation", "rsi_reversion"}
    ) == 1
    assert ledger.pushed_count_since(
        NOW - timedelta(hours=1), {"breakout_20d"}
    ) == 1


def test_latest_signal_price_returns_most_recent(ledger: SignalLedger) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW - timedelta(hours=2))
    ledger.insert(
        Signal(
            ticker="SPY", direction=Direction.BUY, price=105.0, reason="r2",
            strategy_id="momentum_rotation", ts=NOW,
        ),
        pushed=False,
        now=NOW,
    )
    price = ledger.latest_signal_price("momentum_rotation", "SPY", since=NOW - timedelta(hours=24))
    assert price == 105.0   # 取最近一次插入的信号价，即使未实际推送


def test_latest_signal_price_none_when_no_signal_today(ledger: SignalLedger) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW - timedelta(hours=30))
    price = ledger.latest_signal_price("momentum_rotation", "SPY", since=NOW - timedelta(hours=24))
    assert price is None


def test_holdings_roundtrip(ledger: SignalLedger) -> None:
    assert ledger.get_holdings("momentum_rotation") == []
    ledger.set_holdings("momentum_rotation", ["SPY", "QQQ"])
    ledger.set_holdings("momentum_rotation", ["QQQ", "GLD"])   # 覆盖式更新
    assert sorted(ledger.get_holdings("momentum_rotation")) == ["GLD", "QQQ"]


def test_pushed_signals_window_and_order(ledger: SignalLedger) -> None:
    ledger.insert(sig("OLD"), pushed=True, now=NOW - timedelta(days=120))   # 窗口外
    ledger.insert(sig("B"), pushed=True, now=NOW - timedelta(days=2))
    ledger.insert(sig("A"), pushed=True, now=NOW - timedelta(days=1))
    ledger.insert(sig("N"), pushed=False, now=NOW)                          # 未推送不计
    rows = ledger.pushed_signals(NOW - timedelta(days=90))
    assert [r["ticker"] for r in rows] == ["B", "A"]


def test_latest_price_for_direction(ledger: SignalLedger) -> None:
    """P3 卖出持有期盈亏：取该标的最近一次 BUY 信号价, 不限时间窗口。"""
    ledger.insert(sig("MU"), pushed=True, now=NOW - timedelta(days=40))     # BUY @100
    ledger.insert(
        Signal(ticker="MU", direction=Direction.SELL, price=120.0, reason="r",
               strategy_id="momentum_rotation", ts=NOW),
        pushed=True, now=NOW,
    )
    assert ledger.latest_price_for("momentum_rotation", "MU", "buy") == 100.0
    assert ledger.latest_price_for("momentum_rotation", "XX", "buy") is None


def test_signal_rejects_non_finite_price() -> None:
    with pytest.raises(ValueError, match="finite"):
        Signal(
            ticker="BAD",
            direction=Direction.BUY,
            price=math.nan,
            reason="bad feed",
            strategy_id="momentum_rotation",
            ts=NOW,
        )


def test_scan_candidates_replace_and_round_trip(ledger: SignalLedger) -> None:
    scan_date = date(2026, 7, 9)
    ledger.replace_scan_candidates(
        scan_date,
        [
            {
                "ticker": "MSFT",
                "rank": 2,
                "score": 0.31,
                "price": 510.0,
                "extra": {"memberships": ["nasdaq100", "sp500"]},
            },
            {
                "ticker": "AAPL",
                "rank": 1,
                "score": 0.42,
                "price": 220.0,
                "extra": {"entry_low": 215.0, "entry_high": 220.0},
            },
        ],
        as_of=date(2026, 7, 8),
    )

    rows = ledger.latest_scan_candidates(scan_date)

    assert [row["ticker"] for row in rows] == ["AAPL", "MSFT"]
    assert rows[0]["scan_date"] == "2026-07-09"
    assert rows[0]["as_of"] == "2026-07-08"
    assert rows[0]["extra"] == {"entry_low": 215.0, "entry_high": 220.0}
    assert json.loads(str(rows[1]["extra_json"])) == {
        "memberships": ["nasdaq100", "sp500"]
    }


def test_scan_candidates_same_day_replace_removes_old_rows(
    ledger: SignalLedger,
) -> None:
    scan_date = date(2026, 7, 9)
    ledger.replace_scan_candidates(
        scan_date,
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=scan_date,
    )
    ledger.replace_scan_candidates(
        scan_date,
        [{"ticker": "NEW", "rank": 1, "score": 0.2, "price": 20.0}],
        as_of=scan_date,
    )

    assert [row["ticker"] for row in ledger.latest_scan_candidates(scan_date)] == [
        "NEW"
    ]


def test_latest_scan_candidates_uses_most_recent_scan_date(
    ledger: SignalLedger,
) -> None:
    ledger.replace_scan_candidates(
        date(2026, 7, 8),
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=date(2026, 7, 7),
    )
    ledger.replace_scan_candidates(
        date(2026, 7, 9),
        [{"ticker": "NEW", "rank": 1, "score": 0.2, "price": 20.0}],
        as_of=date(2026, 7, 8),
    )

    assert [row["ticker"] for row in ledger.latest_scan_candidates()] == ["NEW"]
    assert [
        row["ticker"]
        for row in ledger.latest_scan_candidates(date(2026, 7, 8))
    ] == ["OLD"]


def test_scan_candidate_schema_is_additive_to_existing_signals(
    ledger: SignalLedger,
) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW)
    ledger.replace_scan_candidates(
        NOW.date(),
        [{"ticker": "AAPL", "rank": 1, "score": 0.5, "price": 220.0}],
        as_of=NOW.date(),
    )

    assert [row["ticker"] for row in ledger.signals_on(NOW.date())] == ["SPY"]
