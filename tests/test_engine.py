from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine, _intraday_snapshot
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


class FakeSource:
    def __init__(self, daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> None:
        self._daily = daily
        self._intraday = intraday

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return self._daily

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        assert self._intraday is not None
        return self._intraday


@pytest.fixture
def env(tmp_path: Path, daily_bars: pd.DataFrame):  # type: ignore[no-untyped-def]
    settings = load_settings()
    strategies = dict(settings.strategies)
    strategies["momentum_rotation"] = {**strategies["momentum_rotation"], "top_n": 2}
    settings = settings.model_copy(
        update={
            "universe": ["AAA", "BBB", "CCC", "DDD"],
            "watchlist": ["AAA"],
            "strategies": strategies,
        }
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    return settings, store, ledger, notifier


def test_premarket_generates_rotation_and_report(env, daily_bars) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    ledger.set_holdings("momentum_rotation", ["CCC"])   # 旧持仓将被轮出
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    # 模拟真实场景：bar 是昨日收盘（约 T+0:00），premarket 在今日 08:00 ET 运行，
    # 两者天然相差 >4h —— 验证去重不会因此被"信号自身 ts 过旧"误判为已过期。
    run_at = last_bar_ts + timedelta(hours=32)
    engine.run_premarket(run_at)

    rows = ledger.signals_on(last_bar_ts.date())
    directions = {(r["ticker"], r["direction"]) for r in rows}
    assert ("AAA", "buy") in directions and ("BBB", "buy") in directions
    assert ("CCC", "sell") in directions                 # 调仓卖出
    holdings = set(ledger.get_holdings("momentum_rotation"))
    assert {"AAA", "BBB"} <= holdings and "CCC" not in holdings
    assert len(notifier.cards) == 1                      # 一张早报卡
    assert "早报" in notifier.cards[0].title


def test_premarket_dedup_second_run_no_push(env, daily_bars) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    run_at = last_bar_ts + timedelta(hours=32)
    engine.run_premarket(run_at)
    n_first = len([r for r in ledger.signals_on(last_bar_ts.date()) if r["pushed"]])
    engine.run_premarket(run_at)                         # 同日重跑
    rows = ledger.signals_on(last_bar_ts.date())
    n_pushed = len([r for r in rows if r["pushed"]])
    assert n_pushed == n_first                           # 4h 窗口内不重复推


def test_intraday_snapshot_appends_partial_day(daily_bars: pd.DataFrame) -> None:
    ts5 = pd.date_range("2026-07-06 13:30", periods=6, freq="5min", tz="UTC")
    idx = pd.MultiIndex.from_product([["AAA"], ts5], names=["ticker", "ts"])
    intraday = pd.DataFrame(
        {"open": 200.0, "high": 201.0, "low": 199.0, "close": 200.5, "volume": 10_000},
        index=idx,
    )
    combined = _intraday_snapshot(daily_bars, intraday, date(2026, 7, 6))
    aaa = combined.xs("AAA", level="ticker")
    assert aaa.index[-1].date() == date(2026, 7, 6)
    assert aaa["volume"].iloc[-1] == 60_000              # 6 根 5min 量累加
    assert aaa["high"].iloc[-1] == 201.0
