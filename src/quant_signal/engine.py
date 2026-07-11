from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
import structlog

from quant_signal.account import AccountProvider, AlpacaPaperAccountProvider
from quant_signal.config import REPO_ROOT, Settings
from quant_signal.datafeed.base import DataSource
from quant_signal.datafeed.earnings import EarningsSource
from quant_signal.datafeed.fundamentals import FundamentalsSource
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.ledger import SignalLedger
from quant_signal.index_universe import IndexUniverseProvider, UniverseCache
from quant_signal.notifier.base import Notifier
from quant_signal.notifier.dedup import DedupResult, apply_dedup
from quant_signal.pipelines.deviation import run as run_deviation_pipeline
from quant_signal.pipelines.enrichment import run as run_enrichment_pipeline
from quant_signal.pipelines.execution_plan import (
    run_daily as run_execution_brief_pipeline,
    run_watch as run_execution_watch_pipeline,
)
from quant_signal.pipelines.intraday import (
    intraday_snapshot as _intraday_snapshot,
    run as run_intraday_pipeline,
)
from quant_signal.pipelines.dataqa import run as run_dataqa_pipeline
from quant_signal.pipelines.market_scan import run as run_market_scan_pipeline
from quant_signal.pipelines.negative_overreaction import run as run_negative_overreaction_pipeline
from quant_signal.pipelines.option_flow import deliver as deliver_option_flow_alerts
from quant_signal.pipelines.option_flow import run as run_option_flow_pipeline
from quant_signal.pipelines.performance import run as run_performance_pipeline
from quant_signal.pipelines.premarket import run as run_premarket_pipeline
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.indicators import (
    chandelier_stop,
    entry_hint,
    expected_move_target,
)
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.momentum_rotation import MomentumRotation
from quant_signal.strategies.rsi_reversion import RsiReversion
from quant_signal.strategies.trend_gate import TrendGateConfig

if TYPE_CHECKING:
    from quant_signal.datafeed.news import NewsSource
    from quant_signal.news_store import NewsStore
    from quant_signal.options_flow import OptionFlowEnricher, OptionFlowSource

log = structlog.get_logger()


class Engine:
    """Dependency assembly and stable facade for scheduled workflow pipelines."""

    def __init__(
        self,
        settings: Settings,
        store: BarStore,
        source: DataSource,
        ledger: SignalLedger,
        notifier: Notifier,
        enrichers: list[object] | None = None,
        earnings_source: EarningsSource | None = None,
        fundamentals_source: FundamentalsSource | None = None,
        news_source: "NewsSource | None" = None,
        news_store: "NewsStore | None" = None,
        index_universe_provider: IndexUniverseProvider | None = None,
        account_provider: AccountProvider | None = None,
        option_flow_source: "OptionFlowSource | None" = None,
        option_flow_enricher: "OptionFlowEnricher | None" = None,
    ) -> None:
        # 财报日历/基本面为可选注入：不注入(如测试)则完全跳过标注，零网络依赖
        self.earnings_source = earnings_source
        self.fundamentals_source = fundamentals_source
        self.news_source = news_source
        self.news_store = news_store
        self.option_flow_source = option_flow_source
        self.option_flow_enricher = option_flow_enricher
        self.settings = settings
        self.store = store
        self.source = source
        self.ledger = ledger
        self.notifier = notifier
        self.enrichers = enrichers or []
        if index_universe_provider is None and settings.index_universe.enabled:
            cache_path = Path(settings.index_universe.cache_path)
            if not cache_path.is_absolute():
                cache_path = REPO_ROOT / cache_path
            index_universe_provider = IndexUniverseProvider(
                cache=UniverseCache(cache_path),
                indices=settings.index_universe.indices,
                refresh_days=settings.index_universe.refresh_days,
                max_stale_days=settings.index_universe.max_stale_days,
            )
        self.index_universe_provider = index_universe_provider
        if (
            account_provider is None
            and settings.execution_plan.enabled
            and settings.execution_plan.account_provider == "alpaca_paper"
            and settings.alpaca_key
            and settings.alpaca_secret
        ):
            account_provider = AlpacaPaperAccountProvider(
                settings.alpaca_key, settings.alpaca_secret
            )
        if (
            account_provider is None
            and settings.execution_plan.enabled
            and settings.execution_plan.account_provider == "screenshot"
        ):
            from quant_signal.screenshot_account import ScreenshotAccountProvider

            account_provider = ScreenshotAccountProvider(
                ledger,
                max_age=timedelta(
                    hours=settings.execution_plan.screenshot_max_age_hours
                ),
            )
        self.account_provider = account_provider
        momentum = settings.strategies["momentum_rotation"]
        breakout = settings.strategies["breakout_20d"]
        self.momentum = MomentumRotation(
            universe=settings.universe,
            lookback_days=int(momentum["lookback_days"]),
            top_n=int(momentum["top_n"]),
            min_dollar_volume=float(momentum["min_dollar_volume"]),
            ticker_currency=settings.international_tickers,
            group_top_n=settings.momentum_group_top_n,
            asset_type=settings.asset_type,
            default_group_top_n=settings.momentum_default_group_top_n,
            leverage_factor=settings.leverage_factor,
        )
        trend = settings.trend_gate
        self.trend_gate_cfg = (
            TrendGateConfig(
                ma_days=trend.ma_days,
                mom_days=trend.mom_days,
                buffer=trend.buffer,
                benchmark=trend.benchmark,
                defensive=tuple(trend.defensive),
            )
            if trend.enabled
            else None
        )
        self.trend_gate_use_mom = trend.use_mom
        self._intl_source = YFinanceSource()
        self.breakout = Breakout20d(
            universe=settings.watchlist,
            high_lookback_days=int(breakout["high_lookback_days"]),
            volume_multiplier=float(breakout["volume_multiplier"]),
        )
        rsi = settings.strategies.get("rsi_reversion", {})
        macd = settings.strategies.get("macd_cross", {})
        bollinger = settings.strategies.get("bollinger_breakout", {})
        self.rsi = RsiReversion(
            universe=settings.universe,
            period=int(rsi.get("period", 14)),
            oversold=float(rsi.get("oversold", 30)),
            overbought=float(rsi.get("overbought", 70)),
        )
        self.macd = MacdCross(
            universe=settings.universe,
            fast=int(macd.get("fast", 12)),
            slow=int(macd.get("slow", 26)),
            signal=int(macd.get("signal", 9)),
        )
        self.bollinger = BollingerBreakout(
            universe=settings.universe,
            period=int(bollinger.get("period", 20)),
            num_std=float(bollinger.get("num_std", 2.0)),
        )

    def _dedup(
        self,
        signals: list[Signal],
        now: datetime,
        channel: Literal["premarket", "intraday", "deviation"],
    ) -> DedupResult:
        config = self.settings.notify
        strategies_by_channel = {
            "premarket": {
                "momentum_rotation",
                "rsi_reversion",
                "macd_cross",
                "bollinger_breakout",
            },
            "intraday": {"breakout_20d"},
            "deviation": {"price_deviation"},
        }
        limit_by_channel = {
            "premarket": config.premarket_hourly_limit,
            "intraday": config.intraday_hourly_limit,
            "deviation": config.deviation_hourly_limit,
        }
        return apply_dedup(
            signals,
            now,
            self.ledger.last_push_by_key(
                now - timedelta(hours=config.dedup_hours)
            ),
            self.ledger.pushed_count_since(
                now - timedelta(hours=1), strategies_by_channel[channel]
            ),
            dedup_hours=config.dedup_hours,
            hourly_limit=limit_by_channel[channel],
        )

    def _refresh_daily(self, now: datetime) -> pd.DataFrame:
        base = set(self.settings.universe) | set(self.settings.watchlist)
        if self.trend_gate_cfg is not None:
            base |= {self.trend_gate_cfg.benchmark} | set(
                self.trend_gate_cfg.defensive
            )
        tickers = sorted(base)
        international = [
            ticker
            for ticker in tickers
            if ticker in self.settings.international_tickers
        ]
        primary = [
            ticker
            for ticker in tickers
            if ticker not in self.settings.international_tickers
        ]
        start = (now - timedelta(days=10)).date()
        end = now.date() + timedelta(days=1)
        if primary:
            fresh = self.source.fetch_daily_bars(primary, start, end)
            self.store.write_daily_bars(fresh, source=self.settings.data_source)
        if international:
            fresh_international = self._intl_source.fetch_daily_bars(
                international, start, end
            )
            self.store.write_daily_bars(fresh_international, source="yfinance")
        return self.store.read_daily_bars(
            tickers, start=now - timedelta(days=400)
        )

    def _refresh_fx_rates(self) -> None:
        currencies = {
            self.settings.international_tickers[ticker]
            for ticker in self.settings.universe
            if ticker in self.settings.international_tickers
        }
        if currencies:
            self.momentum.fx_rates = fetch_usd_rates(currencies)

    def _attach_exit_prices(
        self, signals: list[Signal], bars: pd.DataFrame
    ) -> list[Signal]:
        trend = self.settings.trend_gate
        output: list[Signal] = []
        for signal in signals:
            if signal.direction != Direction.BUY:
                output.append(signal)
                continue
            try:
                ticker_bars = bars.xs(signal.ticker, level="ticker").sort_index()
            except KeyError:
                output.append(signal)
                continue
            stop_loss = chandelier_stop(
                ticker_bars["high"],
                ticker_bars["low"],
                ticker_bars["close"],
                lookback=trend.chandelier_lookback,
                atr_period=trend.chandelier_atr,
                mult=trend.chandelier_mult,
            )
            take_profit = expected_move_target(
                ticker_bars["close"], trend.tp_vol_lookback, trend.tp_horizon
            )
            extra = dict(signal.extra or {})
            if stop_loss is not None:
                extra["stop_loss"] = round(stop_loss, 2)
            if take_profit is not None:
                extra["take_profit"] = round(take_profit, 2)
            hint = entry_hint(
                ticker_bars["high"], ticker_bars["low"], ticker_bars["close"]
            )
            if hint is not None:
                entry_low, entry_high, overheated = hint
                extra["entry_low"] = round(entry_low, 2)
                extra["entry_high"] = round(entry_high, 2)
                if overheated:
                    extra["overheat"] = True
                # 策略没给 target_buy 的(动量轮动)，目标买入价=回踩带下沿
                extra.setdefault("target_buy", round(entry_low, 2))
            output.append(
                replace(signal, extra=extra)
                if extra != (signal.extra or {})
                else signal
            )
        return output

    def _fetch_live_price(self, ticker: str) -> float | None:
        try:
            return self._intl_source.fetch_live_price(ticker)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "live_price.fetch_failed", ticker=ticker, error=str(error)
            )
            return None

    def _fetch_live_prices(
        self, tickers: set[str]
    ) -> dict[str, float | None]:
        ordered = sorted(tickers)
        try:
            fetched = self._intl_source.fetch_live_prices(ordered)
            return {ticker: fetched.get(ticker) for ticker in ordered}
        except Exception as error:  # noqa: BLE001
            log.warning(
                "live_price.batch_failed", tickers=ordered, error=str(error)
            )
            return {ticker: None for ticker in ordered}

    def run_premarket(self, now: datetime) -> None:
        run_premarket_pipeline(self, now)

    def run_intraday(self, now: datetime) -> None:
        run_intraday_pipeline(self, now)

    def run_watch_deviation(self, now: datetime) -> None:
        run_deviation_pipeline(self, now)

    def run_enrichment(self, now: datetime) -> None:
        run_enrichment_pipeline(self, now)

    def run_performance(self, now: datetime) -> None:
        run_performance_pipeline(self, now)

    def run_data_qa(self, now: datetime) -> None:
        run_dataqa_pipeline(self, now)

    def run_market_scan(self, now: datetime) -> None:
        run_market_scan_pipeline(self, now)

    def run_negative_overreaction(self, now: datetime) -> None:
        run_negative_overreaction_pipeline(self, now)

    def run_execution_brief(self, now: datetime) -> None:
        run_execution_brief_pipeline(self, now)

    def run_execution_watch(self, now: datetime) -> None:
        run_execution_watch_pipeline(self, now)

    def run_option_flow(self, now: datetime, *, force_summary: bool = False) -> None:
        run_option_flow_pipeline(self, now, force_summary=force_summary)

    def run_option_flow_delivery(self, now: datetime) -> None:
        deliver_option_flow_alerts(self, now)
