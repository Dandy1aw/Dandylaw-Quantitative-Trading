"""台账：option_intel_daily 落库/清理 + 全策略持仓查询。"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_signal.ledger import SignalLedger
from quant_signal.options_intel import OptionIntel

NOW = datetime(2026, 7, 10, 20, 40, tzinfo=UTC)


@pytest.fixture
def ledger(tmp_path: Path) -> SignalLedger:
    return SignalLedger(tmp_path / "signals.db")


def intel(symbol: str = "MU", *, atm_iv: float | None = 0.5) -> OptionIntel:
    return OptionIntel(
        symbol=symbol,
        spot=Decimal("100"),
        session=date(2026, 7, 10),
        expected_move_pct=0.045,
        expected_move_expiry=date(2026, 7, 24),
        earnings_move_pct=None,
        earnings_move_expiry=None,
        atm_iv=atm_iv,
        realized_vol_20d=0.40,
        pc_volume_ratio=0.7,
        pc_oi_ratio=0.9,
        top_oi_strikes=(),
        earnings_date=None,
        data_note=None,
    )


def test_save_and_upsert_option_intel_daily(ledger: SignalLedger) -> None:
    ledger.save_option_intel_daily(intel(), now=NOW)
    ledger.save_option_intel_daily(intel(atm_iv=0.6), now=NOW)  # 同日同标的覆盖

    rows = ledger.option_intel_history("MU")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_date"] == "2026-07-10"
    assert row["atm_iv"] == 0.6
    assert row["expected_move_pct"] == 0.045
    assert row["spot"] == "100"


def test_prune_option_intel_respects_boundary(ledger: SignalLedger) -> None:
    ledger.save_option_intel_daily(intel(), now=NOW - timedelta(days=10))
    old = replace(intel("SMH"), session=date(2026, 5, 1))
    ledger.save_option_intel_daily(old, now=NOW - timedelta(days=70))

    deleted = ledger.prune_option_intel(NOW - timedelta(days=30))
    assert deleted == 1
    assert ledger.option_intel_history("MU")
    assert not ledger.option_intel_history("SMH")


def test_all_held_tickers_unions_strategies(ledger: SignalLedger) -> None:
    ledger.set_holdings("momentum_rotation", ["MU", "SMH"])
    ledger.set_holdings("other", ["SMH", "AMD"])
    assert ledger.all_held_tickers() == ["AMD", "MU", "SMH"]
