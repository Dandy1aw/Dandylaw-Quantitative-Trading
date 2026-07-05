from pathlib import Path

from quant_signal.ledger import SignalLedger
from quant_signal.seed_holdings import parse_tickers, seed


def test_seed_sets_holdings(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "s.db")
    result = seed(ledger, "momentum_rotation", ["SPY", "QQQ", "MU"])
    assert sorted(result) == ["MU", "QQQ", "SPY"]
    assert sorted(ledger.get_holdings("momentum_rotation")) == ["MU", "QQQ", "SPY"]


def test_seed_overwrites_previous(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "s.db")
    seed(ledger, "momentum_rotation", ["SPY"])
    seed(ledger, "momentum_rotation", ["QQQ", "MU"])
    assert sorted(ledger.get_holdings("momentum_rotation")) == ["MU", "QQQ"]


def test_parse_tickers_trims_and_uppercases() -> None:
    assert parse_tickers(" spy, qqq ,mu ") == ["SPY", "QQQ", "MU"]


def test_parse_tickers_drops_empty_entries() -> None:
    assert parse_tickers("spy,,qqq,") == ["SPY", "QQQ"]
