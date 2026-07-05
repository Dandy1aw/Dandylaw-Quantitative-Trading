from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_signal.ledger import SignalLedger
from quant_signal.strategies.base import Direction, Signal, dedup_key

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def sig(ticker: str = "SPY", ts: datetime = NOW) -> Signal:
    return Signal(
        ticker=ticker,
        direction=Direction.BUY,
        price=100.0,
        reason="r",
        strategy_id="momentum_rotation",
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
