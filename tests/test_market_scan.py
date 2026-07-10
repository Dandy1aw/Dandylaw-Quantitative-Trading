from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from conftest import make_test_settings

from quant_signal.config import IndexUniverseSettings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.index_universe import merge_members
from quant_signal.ledger import SignalLedger

NOW = datetime(2026, 7, 8, 11, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def _bars(
    tickers: dict[str, float], n: int = 140, end: str = "2026-07-08"
) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=n, tz="UTC")
    frames = []
    for ticker, drift in tickers.items():
        close = 50.0 * np.cumprod(np.full(n, 1 + drift))
        frames.append(pd.DataFrame(
            {"open": close, "high": close * 1.01, "low": close * 0.99,
             "close": close, "volume": 5e6},
            index=pd.MultiIndex.from_product([[ticker], idx], names=["ticker", "ts"]),
        ))
    return pd.concat(frames)


def _index_bars(tickers: dict[str, float], n: int = 140) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-07", periods=n, tz="UTC")
    frames = []
    for ticker, drift in tickers.items():
        returns = 1 + drift + 0.002 * np.sin(np.arange(n) / 3)
        close = 50.0 * np.cumprod(returns)
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
    settings = make_test_settings(
        watchlist=[], index_universe=IndexUniverseSettings(enabled=False)
    )
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

    settings = make_test_settings(
        watchlist=[], index_universe=IndexUniverseSettings(enabled=False)
    )
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
    settings = make_test_settings(
        watchlist=[], index_universe=IndexUniverseSettings(enabled=False)
    )
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


class FakeUniverseProvider:
    def __init__(self, memberships: dict[str, set[str]]) -> None:
        self.snapshot = merge_members(memberships, NOW, as_of=date(2026, 7, 7))
        self.loads: list[datetime] = []

    def load(self, now: datetime):  # type: ignore[no-untyped-def]
        self.loads.append(now)
        return self.snapshot


class FakeSipSource:
    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars
        self.sip_calls: list[tuple[list[str], date, date]] = []
        self.lister_calls = 0

    def list_active_symbols(self) -> list[str]:
        self.lister_calls += 1
        raise AssertionError("index mode must not enumerate all active symbols")

    def fetch_sip_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        self.sip_calls.append((list(tickers), start, end))
        mask = self._bars.index.get_level_values("ticker").isin(tickers)
        return self._bars[mask]

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        raise AssertionError("index mode must use the SIP method")

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _index_settings(*, min_coverage: float = 0.8):  # type: ignore[no-untyped-def]
    return make_test_settings(
        watchlist=[],
        index_universe={
            "enabled": True,
            "indices": ["sp500", "nasdaq100"],
            "scan_top_n": 20,
            "execution_top_n": 2,
            "min_coverage": min_coverage,
            "min_dollar_volume": 1,
        },
    )


def test_index_market_scan_is_bounded_uses_sip_and_caches_all_bars(
    tmp_path: Path,
) -> None:
    bars = _index_bars({"HOT": 0.006, "MEH": 0.001, "COLD": -0.001})
    provider = FakeUniverseProvider(
        {"sp500": {"HOT", "MEH"}, "nasdaq100": {"HOT", "COLD"}}
    )
    source = FakeSipSource(bars)
    store = BarStore(tmp_path / "bars.duckdb")
    ledger = SignalLedger(tmp_path / "signals.db")
    notifier = FakeNotifier()
    engine = Engine(
        _index_settings(),
        store,
        source,  # type: ignore[arg-type]
        ledger,
        notifier,
        index_universe_provider=provider,  # type: ignore[arg-type]
    )

    engine.run_market_scan(NOW)

    assert source.lister_calls == 0
    assert provider.loads == [NOW]
    assert [call[0] for call in source.sip_calls] == [["COLD", "HOT", "MEH"]]
    assert all(store.daily_bar_count(ticker) == 140 for ticker in ("HOT", "MEH", "COLD"))
    candidates = ledger.latest_scan_candidates(NOW.date())
    assert [row["ticker"] for row in candidates] == ["HOT", "MEH", "COLD"]
    assert candidates[0]["extra"]["memberships"] == ["nasdaq100", "sp500"]
    for field in (
        "entry_low",
        "entry_high",
        "target_buy",
        "stop_loss",
        "take_profit",
    ):
        assert field in candidates[0]["extra"]
        assert field in candidates[1]["extra"]
        assert field not in candidates[2]["extra"]
    assert notifier.cards
    scan_signals = [
        row for row in ledger.signals_on(NOW.date())
        if row["strategy_id"] == "market_scan"
    ]
    assert [row["ticker"] for row in scan_signals] == ["HOT"]


def test_index_market_scan_fails_closed_below_coverage_floor(
    tmp_path: Path,
) -> None:
    bars = _index_bars({"A": 0.004, "B": 0.003, "C": 0.002})
    provider = FakeUniverseProvider(
        {"sp500": {"A", "B"}, "nasdaq100": {"C", "MISS"}}
    )
    source = FakeSipSource(bars)
    store = BarStore(tmp_path / "bars.duckdb")
    ledger = SignalLedger(tmp_path / "signals.db")
    notifier = FakeNotifier()
    engine = Engine(
        _index_settings(min_coverage=0.8),
        store,
        source,  # type: ignore[arg-type]
        ledger,
        notifier,
        index_universe_provider=provider,  # type: ignore[arg-type]
    )

    engine.run_market_scan(NOW)

    assert source.lister_calls == 0
    assert all(store.daily_bar_count(ticker) == 140 for ticker in ("A", "B", "C"))
    assert ledger.latest_scan_candidates(NOW.date()) == []
    assert [
        row for row in ledger.signals_on(NOW.date())
        if row["strategy_id"] == "market_scan"
    ] == []
    assert notifier.cards == []


def test_index_market_scan_universe_failure_marks_today_empty(
    tmp_path: Path,
) -> None:
    class FailingProvider:
        def load(self, now: datetime):  # type: ignore[no-untyped-def]
            raise RuntimeError("stale universe")

    ledger = SignalLedger(tmp_path / "signals.db")
    ledger.replace_scan_candidates(
        date(2026, 7, 7),
        [{"ticker": "OLD", "rank": 1, "score": 0.2, "price": 20.0}],
        as_of=date(2026, 7, 6),
    )
    source = FakeSipSource(_index_bars({"A": 0.004}))
    engine = Engine(
        _index_settings(),
        BarStore(tmp_path / "bars.duckdb"),
        source,  # type: ignore[arg-type]
        ledger,
        FakeNotifier(),
        index_universe_provider=FailingProvider(),  # type: ignore[arg-type]
    )

    engine.run_market_scan(NOW)

    assert ledger.latest_scan_candidates() == []
    assert source.lister_calls == 0


def test_index_market_scan_blocks_invalid_execution_price_order(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    bars = _index_bars({"A": 0.004, "B": 0.003, "C": 0.002})
    provider = FakeUniverseProvider(
        {"sp500": {"A", "B"}, "nasdaq100": {"A", "C"}}
    )
    source = FakeSipSource(bars)
    monkeypatch.setattr("quant_signal.pipelines.market_scan.chandelier_stop", lambda *args: 999.0)
    monkeypatch.setattr("quant_signal.pipelines.market_scan.expected_move_target", lambda *args: 1.0)
    ledger = SignalLedger(tmp_path / "signals.db")
    engine = Engine(
        _index_settings(),
        BarStore(tmp_path / "bars.duckdb"),
        source,  # type: ignore[arg-type]
        ledger,
        FakeNotifier(),
        index_universe_provider=provider,  # type: ignore[arg-type]
    )

    engine.run_market_scan(NOW)

    first = ledger.latest_scan_candidates(NOW.date())[0]
    assert first["extra"]["block_reason"] == "INVALID_PRICE_ORDER"
