from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.datafeed.store import BarStore
from quant_signal.forward_evaluation import evaluate_candidate_forward_returns
from quant_signal.ledger import SignalLedger


def _bars() -> pd.DataFrame:
    sessions = pd.date_range("2026-01-05", periods=63, freq="B", tz="UTC")
    close = [101.0 + value for value in range(63)]
    candidate = pd.DataFrame(
        {
            "open": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "close": close,
            "volume": 1_000_000,
        },
        index=pd.MultiIndex.from_product(
            [["MEGA"], sessions], names=["ticker", "ts"]
        ),
    )
    benchmark_sessions = pd.DatetimeIndex(
        [pd.Timestamp("2026-01-02", tz="UTC"), *sessions]
    )
    benchmark_close = [100.0, *[100.0 + (value + 1) * 0.2 for value in range(63)]]
    benchmark = pd.DataFrame(
        {
            "open": benchmark_close,
            "high": benchmark_close,
            "low": benchmark_close,
            "close": benchmark_close,
            "volume": 2_000_000,
        },
        index=pd.MultiIndex.from_product(
            [["QQQ"], benchmark_sessions], names=["ticker", "ts"]
        ),
    )
    return pd.concat([candidate, benchmark]).sort_index()


def test_forward_evaluation_is_session_based_and_immutable(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    store = BarStore(tmp_path / "bars.duckdb")
    captured = datetime(2026, 1, 2, 23, tzinfo=timezone.utc)
    ledger.replace_candidate_lane_snapshot(
        "DAILY_ACTION",
        date(2026, 1, 2),
        [
            {
                "ticker": "MEGA",
                "lane": "TREND_CONTINUATION",
                "price": 100.0,
                "score": 0.9,
            }
        ],
        now=captured,
    )
    store.write_daily_bars(_bars(), source="test")

    assert evaluate_candidate_forward_returns(
        ledger, store, now=datetime(2026, 4, 30, tzinfo=timezone.utc)
    ) == 5
    assert evaluate_candidate_forward_returns(
        ledger, store, now=datetime(2026, 5, 1, tzinfo=timezone.utc)
    ) == 0

    rows = ledger.candidate_forward_evaluations()
    assert [row["horizon_sessions"] for row in rows] == [5, 10, 20, 21, 63]
    assert rows[0]["exit_price"] == 105.0
    assert rows[0]["return_pct"] == 0.05
    assert rows[0]["benchmark_return_pct"] == 0.01
    assert rows[0]["excess_return_pct"] == 0.04
    assert rows[0]["max_favorable_excursion_pct"] == 0.06
    assert rows[0]["max_adverse_excursion_pct"] == 0.0
    assert rows[0]["net_return_pct"] < rows[0]["return_pct"]
    assert rows[0]["transaction_cost_bps"] == 10.0
    assert rows[-1]["exit_price"] == 163.0
    assert rows[-1]["return_pct"] == 0.63
