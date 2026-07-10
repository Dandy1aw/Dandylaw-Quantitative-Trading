from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger

NOW = datetime(2026, 7, 8, 11, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def _bars(tickers: dict[str, float], n: int = 140) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-08", periods=n, tz="UTC")
    frames = []
    for ticker, drift in tickers.items():
        close = 50.0 * np.cumprod(np.full(n, 1 + drift))
        frames.append(pd.DataFrame(
            {"open": close, "high": close * 1.01, "low": close * 0.99,
             "close": close, "volume": 5e6},
            index=pd.MultiIndex.from_product([[ticker], idx], names=["ticker", "ts"]),
        ))
    return pd.concat(frames)


class FakeScanSource:
    """带 list_active_symbols 的假数据源(模拟 Alpaca)。"""

    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars

    def list_active_symbols(self) -> list[str]:
        return sorted(set(self._bars.index.get_level_values("ticker")))

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        mask = self._bars.index.get_level_values("ticker").isin(tickers)
        return self._bars[mask]

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_market_scan_pushes_top1_and_records_signal(tmp_path: Path) -> None:
    settings = load_settings().model_copy(update={"watchlist": []})
    bars = _bars({"HOT": 0.01, "MEH": 0.0, "COLD": -0.005})
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    engine = Engine(settings, store, FakeScanSource(bars), ledger, notifier)

    engine.run_market_scan(NOW)

    titles = [c.title for c in notifier.cards]  # type: ignore[attr-defined]
    assert any("全市场扫描" in t for t in titles)
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "HOT" in body and "目标买入价" in body
    rows = ledger.signals_on(NOW.date())
    scan_rows = [r for r in rows if r["strategy_id"] == "market_scan"]
    assert len(scan_rows) == 1 and scan_rows[0]["ticker"] == "HOT"


def test_market_scan_skips_source_without_asset_list(tmp_path: Path) -> None:
    class Plain:
        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            raise AssertionError("不应被调用")

        def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    settings = load_settings().model_copy(update={"watchlist": []})
    notifier = FakeNotifier()
    engine = Engine(settings, BarStore(tmp_path / "b.duckdb"),
                    Plain(), SignalLedger(tmp_path / "s.db"), notifier)
    engine.run_market_scan(NOW)
    assert notifier.cards == []


def test_market_scan_uses_full_volume_source_when_primary_is_partial(tmp_path: Path) -> None:
    bars = _bars({"HOT": 0.01, "MEH": 0.0, "COLD": -0.005})

    class PartialSource(FakeScanSource):
        partial_market_volume = True

        def __init__(self, data: pd.DataFrame) -> None:
            super().__init__(data)
            self.fetches = 0

        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            self.fetches += 1
            raise AssertionError("部分市场成交量不应参与扫描")

    primary = PartialSource(bars)
    full = FakeScanSource(bars)
    settings = load_settings().model_copy(update={"watchlist": []})
    notifier = FakeNotifier()
    engine = Engine(
        settings,
        BarStore(tmp_path / "b.duckdb"),
        primary,
        SignalLedger(tmp_path / "s.db"),
        notifier,
    )
    engine._intl_source = full  # type: ignore[assignment]

    engine.run_market_scan(NOW)

    assert primary.fetches == 0
    assert notifier.cards
