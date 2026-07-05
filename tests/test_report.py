from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import CardKind
from quant_signal.report import build_daily_report
from quant_signal.strategies.base import Direction, Signal


def test_daily_report_counts_and_theoretical_return(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "s.db")
    store = BarStore(tmp_path / "b.duckdb")
    day = date(2026, 7, 6)
    ts = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    ledger.insert(
        Signal(
            ticker="NVDA",
            direction=Direction.BUY,
            price=100.0,
            reason="r",
            strategy_id="breakout_20d",
            ts=ts,
        ),
        pushed=True,
        now=ts,
    )
    idx = pd.MultiIndex.from_tuples(
        [("NVDA", pd.Timestamp("2026-07-06", tz="UTC"))], names=["ticker", "ts"]
    )
    store.write_daily_bars(
        pd.DataFrame(
            {
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 1_000_000,
            },
            index=idx,
        ),
        source="test",
    )
    card = build_daily_report(ledger, store, day)
    assert card.kind is CardKind.REPORT
    assert "1" in card.body_md          # 信号数
    assert "+5.0%" in card.body_md      # (105-100)/100


def test_daily_report_empty_day(tmp_path: Path) -> None:
    card = build_daily_report(
        SignalLedger(tmp_path / "s.db"), BarStore(tmp_path / "b.duckdb"), date(2026, 7, 6)
    )
    assert "无信号" in card.body_md
