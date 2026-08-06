from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from quant_signal.config import AIBriefingSettings, EnrichmentSettings, NotifySettings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine, _intraday_snapshot
from quant_signal.pipelines import premarket as premarket_pipeline
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.strategies.base import Direction, Signal
from conftest import make_test_settings


class FakeNotifier:
    def __init__(self, success: bool = True) -> None:
        self.cards: list[Card] = []
        self.success = success

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return self.success


class FakeSource:
    def __init__(self, daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> None:
        self._daily = daily
        self._intraday = intraday

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return self._daily

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        assert self._intraday is not None
        return self._intraday


class FakeLiveSource:
    """替身：现价统一走 yfinance(prepost)，测试里注入固定现价。"""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    def fetch_live_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def fetch_live_prices(self, tickers: list[str]) -> dict[str, float]:
        return {ticker: self._prices[ticker] for ticker in tickers if ticker in self._prices}


@pytest.fixture
def env(
    tmp_path: Path,
    daily_bars: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "quant_signal.engine.YFinanceSource", lambda: FakeLiveSource({})
    )
    settings = make_test_settings()
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
    titles = [c.title for c in notifier.cards]
    assert any("美股组" in t for t in titles)            # 合成标的均为美股，出美股组卡
    assert "📊 动量全池榜单" in titles
    assert all("盘前早报" in t or "【重要】" in t or "动量全池榜单" in t for t in titles)


def test_action_card_only_premarket_calculates_without_duplicate_pushes(
    env, daily_bars
) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    settings = settings.model_copy(
        update={"notify": NotifySettings(action_card_only=True)}
    )
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    run_at = daily_bars.index.get_level_values("ts").max() + timedelta(hours=32)

    engine.run_premarket(run_at)

    assert notifier.cards == []
    assert ledger.get_strategy_targets("momentum_rotation")


def test_premarket_does_not_send_standalone_ai_briefing_card(
    env, daily_bars, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    settings = settings.model_copy(
        update={
            "ai_briefing": AIBriefingSettings(
                enabled=True, provider="claude_code_cli"
            )
        }
    )
    def fake_ai_briefing(settings, context):  # type: ignore[no-untyped-def]
        raise AssertionError("premarket must not call standalone AI briefing")

    monkeypatch.setattr(premarket_pipeline, "run_ai_briefing", fake_ai_briefing)

    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    engine.run_premarket(last_bar_ts + timedelta(hours=32))

    titles = [card.title for card in notifier.cards]
    assert "🤖 AI早报观点" not in titles


def test_premarket_skips_ai_briefing_card_when_cli_returns_none(
    env, daily_bars, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    settings = settings.model_copy(
        update={
            "ai_briefing": AIBriefingSettings(
                enabled=True, provider="codex_cli"
            )
        }
    )

    def fake_ai_briefing(settings, context):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(premarket_pipeline, "run_ai_briefing", fake_ai_briefing)

    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    engine.run_premarket(last_bar_ts + timedelta(hours=32))

    assert all("AI早报观点" not in card.title for card in notifier.cards)
    assert notifier.cards


def test_premarket_report_shows_strategy_column_for_multiple_strategies(
    env, daily_bars
) -> None:  # type: ignore[no-untyped-def]
    """conftest 的 daily_bars 全程零波动持续上涨，RSI 必然对所有标的判定超买(SELL)，
    验证早报能同时展示动量轮动和 RSI 回归两个不同策略来源的信号。"""
    settings, store, ledger, notifier = env
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    engine.run_premarket(last_bar_ts + timedelta(hours=32))

    body = "\n".join(c.body_md for c in notifier.cards)
    assert "动量轮动" in body
    assert "RSI回归" in body

    rows = ledger.signals_on(last_bar_ts.date())
    strategy_ids = {r["strategy_id"] for r in rows}
    assert "momentum_rotation" in strategy_ids and "rsi_reversion" in strategy_ids


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


def test_premarket_failed_delivery_is_not_recorded_as_pushed(
    env, daily_bars, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    notifier.success = False
    monkeypatch.setattr(
        "quant_signal.engine.YFinanceSource", lambda: FakeLiveSource({})
    )
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()

    engine.run_premarket(last_bar_ts + timedelta(hours=32))

    rows = ledger.signals_on(last_bar_ts.date())
    assert rows
    assert not any(bool(row["pushed"]) for row in rows)


def test_premarket_routes_international_ticker_and_refreshes_fx(
    tmp_path: Path, daily_bars: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """universe 里的国际标的必须走独立的 yfinance 源，且构造后每次 premarket 都刷新汇率。"""
    aaa_only = daily_bars.loc[daily_bars.index.get_level_values("ticker") == "AAA"]
    krt_bars = aaa_only.rename_axis(index={"ticker": "ticker"}).copy()
    krt_bars.index = pd.MultiIndex.from_tuples(
        [("KRT", ts) for ts in krt_bars.index.get_level_values("ts")], names=["ticker", "ts"]
    )

    settings = make_test_settings(
        universe=["AAA", "KRT"], watchlist=[],
        international_tickers={"KRT": "KRW"},
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()

    fx_calls: list[set[str]] = []
    monkeypatch.setattr(
        "quant_signal.engine.fetch_usd_rates",
        lambda currencies: (fx_calls.append(set(currencies)), {"KRW": 1300.0})[1],
    )
    monkeypatch.setattr("quant_signal.engine.YFinanceSource", lambda: FakeSource(krt_bars))

    engine = Engine(settings, store, FakeSource(aaa_only), ledger, notifier)
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    engine.run_premarket(last_bar_ts + timedelta(hours=1))

    assert fx_calls == [{"KRW"}]
    assert engine.momentum.fx_rates == {"KRW": 1300.0}
    stored = store.read_daily_bars(["KRT"])
    assert not stored.empty   # 国际标的数据确实经由独立的 yfinance 源写入


def test_premarket_report_includes_live_price(env, daily_bars, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    last_bar_ts = daily_bars.index.get_level_values("ts").max()
    # 现价统一走 yfinance(prepost)：用假的 live source 提供 AAA 的现价，BBB 无价
    monkeypatch.setattr(
        "quant_signal.engine.YFinanceSource", lambda: FakeLiveSource({"AAA": 999.0})
    )
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    engine.run_premarket(last_bar_ts + timedelta(hours=32))

    body = "\n".join(c.body_md for c in notifier.cards)
    assert "999.00" in body   # AAA 的现价(含盘前盘后)被抓取并展示
    assert "-" in body        # BBB 没有现价，展示为 "-"


def test_watch_deviation_pushes_alert_on_large_move(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = make_test_settings(universe=["AAA"], watchlist=[])
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    now = datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)

    ledger.set_holdings("momentum_rotation", ["AAA"])
    ledger.insert(
        Signal(
            ticker="AAA", direction=Direction.BUY, price=100.0, reason="r",
            strategy_id="momentum_rotation", ts=now - timedelta(hours=1),
        ),
        pushed=True,
        now=now - timedelta(hours=1),
    )

    # 现价走 yfinance(prepost)：注入 AAA=105（较参考价 100 偏离 +5%，触发告警）
    monkeypatch.setattr(
        "quant_signal.engine.YFinanceSource", lambda: FakeLiveSource({"AAA": 105.0})
    )
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_watch_deviation(now)

    rows = ledger.signals_on(now.date())
    dev_rows = [r for r in rows if r["strategy_id"] == "price_deviation"]
    assert len(dev_rows) == 1 and dev_rows[0]["direction"] == "buy"
    assert len(notifier.cards) == 1


def test_watch_deviation_failed_delivery_is_not_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_test_settings(universe=["AAA"], watchlist=[])
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier(success=False)
    now = datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)
    ledger.set_holdings("momentum_rotation", ["AAA"])
    ledger.insert(
        Signal(
            ticker="AAA", direction=Direction.BUY, price=100.0, reason="r",
            strategy_id="momentum_rotation", ts=now - timedelta(hours=1),
        ),
        pushed=True,
        now=now - timedelta(hours=1),
    )
    monkeypatch.setattr(
        "quant_signal.engine.YFinanceSource", lambda: FakeLiveSource({"AAA": 105.0})
    )
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)

    engine.run_watch_deviation(now)

    row = next(r for r in ledger.signals_on(now.date()) if r["strategy_id"] == "price_deviation")
    assert not bool(row["pushed"])


def test_intraday_failed_delivery_is_not_pushed(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    notifier.success = False
    now = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    empty_index = pd.MultiIndex.from_arrays([[], []], names=["ticker", "ts"])
    empty_bars = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"], index=empty_index
    )
    engine = Engine(settings, store, FakeSource(pd.DataFrame(), empty_bars), ledger, notifier)
    monkeypatch.setattr(
        engine.breakout,
        "generate",
        lambda bars: [
            Signal(
                ticker="AAA", direction=Direction.BUY, price=100.0,
                reason="breakout", strategy_id="breakout_20d", ts=now,
            )
        ],
    )

    engine.run_intraday(now)

    row = next(r for r in ledger.signals_on(now.date()) if r["strategy_id"] == "breakout_20d")
    assert not bool(row["pushed"])


def test_premarket_quota_is_not_consumed_by_intraday_signals(env) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    now = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    for i in range(10):
        ledger.insert(
            Signal(
                ticker=f"I{i}", direction=Direction.BUY, price=100.0,
                reason="breakout", strategy_id="breakout_20d", ts=now,
            ),
            pushed=True,
            now=now,
        )
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    candidate = Signal(
        ticker="AAA", direction=Direction.BUY, price=100.0,
        reason="momentum", strategy_id="momentum_rotation", ts=now,
    )

    result = engine._dedup([candidate], now, channel="premarket")

    assert result.to_push == [candidate]


def test_watch_deviation_no_holdings_is_noop(tmp_path: Path) -> None:
    settings = make_test_settings(universe=["AAA"], watchlist=[])
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_watch_deviation(datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc))
    assert notifier.cards == []


def test_run_enrichment_skips_when_disabled(tmp_path: Path) -> None:
    disabled = EnrichmentSettings(enabled=False)
    settings = make_test_settings(universe=["AAA"], watchlist=[], enrichment=disabled)
    assert settings.enrichment.enabled is False   # 本测试显式关闭，验证跳过分支
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    ledger.set_holdings("momentum_rotation", ["AAA"])
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_enrichment(datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc))
    assert notifier.cards == []


def test_run_enrichment_pushes_card_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enrichment_cfg = EnrichmentSettings(
        enabled=True, uzi_run_py="C:/fake/run.py", python_exe="python",
        depth="lite", timeout_seconds=120, max_tickers=8,
    )
    settings = make_test_settings(
        universe=["AAA"], watchlist=[], enrichment=enrichment_cfg
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    now = datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)

    ledger.set_holdings("momentum_rotation", ["AAA"])
    ledger.insert(
        Signal(ticker="AAA", direction=Direction.BUY, price=100.0, reason="r",
               strategy_id="momentum_rotation", ts=now),
        pushed=True, now=now,
    )

    calls: list[str] = []

    def fake_run_uzi_analysis(ticker, run_py_path, python_exe, depth, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append(ticker)
        return {
            "ticker": ticker, "name": "AAA Inc.", "overall_score": 80.0,
            "verdict_label": "积极", "panel_consensus": 75.0, "risks": [],
        }

    monkeypatch.setattr(
        "quant_signal.pipelines.enrichment.run_uzi_analysis", fake_run_uzi_analysis
    )

    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_enrichment(now)

    assert calls == ["AAA"]


def test_run_enrichment_skips_international_tickers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UZI 只支持美股，港股/韩股会超时/无结果——enrichment 应跳过它们，不白等。"""
    enrichment_cfg = EnrichmentSettings(
        enabled=True, uzi_run_py="C:/fake/run.py", python_exe="python",
        depth="lite", timeout_seconds=120, max_tickers=8,
    )
    settings = make_test_settings(
        universe=["AAA", "000660.KS"], watchlist=[],
        international_tickers={"000660.KS": "KRW"},
        enrichment=enrichment_cfg,
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    now = datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)
    ledger.set_holdings("momentum_rotation", ["AAA", "000660.KS"])

    calls: list[str] = []

    def fake_run_uzi_analysis(ticker, run_py_path, python_exe, depth, timeout_seconds):  # type: ignore[no-untyped-def]
        calls.append(ticker)
        return {"ticker": ticker, "name": "x", "overall_score": 80.0,
                "verdict_label": "积极", "panel_consensus": 75.0, "risks": []}

    monkeypatch.setattr(
        "quant_signal.pipelines.enrichment.run_uzi_analysis", fake_run_uzi_analysis
    )
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_enrichment(now)

    assert calls == ["AAA"]   # 韩股 000660.KS 被跳过
    assert len(notifier.cards) == 1
    assert "深度分析" in notifier.cards[0].title


def test_run_enrichment_no_watch_set_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enrichment_cfg = EnrichmentSettings(
        enabled=True, uzi_run_py="C:/fake/run.py", python_exe="python",
        depth="lite", timeout_seconds=120, max_tickers=8,
    )
    settings = make_test_settings(
        universe=["AAA"], watchlist=[], enrichment=enrichment_cfg
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()

    def fail_if_called(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("不该被调用：watch_set 为空")

    monkeypatch.setattr(
        "quant_signal.pipelines.enrichment.run_uzi_analysis", fail_if_called
    )
    engine = Engine(settings, store, FakeSource(pd.DataFrame()), ledger, notifier)
    engine.run_enrichment(datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc))
    assert notifier.cards == []


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


def test_premarket_latest_close_skips_nan_tail(daily_bars: pd.DataFrame) -> None:
    aaa = daily_bars.xs("AAA", level="ticker").copy()
    aaa.iloc[-1, aaa.columns.get_loc("close")] = float("nan")
    bars = pd.concat({"AAA": aaa}, names=["ticker"])

    price = premarket_pipeline._latest_finite_close(bars, "AAA")

    assert price == pytest.approx(float(aaa["close"].dropna().iloc[-1]))
