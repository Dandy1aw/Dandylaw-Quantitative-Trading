from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import structlog

from quant_signal.config import Settings
from quant_signal.datafeed.base import DataSource
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Notifier
from quant_signal.notifier.cards import alert_card, report_card, signal_card
from quant_signal.notifier.dedup import apply_dedup
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.momentum_rotation import MomentumRotation

log = structlog.get_logger()


def _intraday_snapshot(
    daily: pd.DataFrame, intraday: pd.DataFrame, day: date
) -> pd.DataFrame:
    """把当日 5min bar 聚合成一根'进行中'日 bar，追加到各票日线尾部。"""
    frames = [daily]
    day_start = pd.Timestamp(day, tz="UTC")
    cur = intraday[intraday.index.get_level_values("ts") >= day_start]
    for ticker in cur.index.get_level_values("ticker").unique():
        tb = cur.xs(ticker, level="ticker").sort_index()
        if tb.empty:
            continue
        row = pd.DataFrame(
            {
                "open": [float(tb["open"].iloc[0])],
                "high": [float(tb["high"].max())],
                "low": [float(tb["low"].min())],
                "close": [float(tb["close"].iloc[-1])],
                "volume": [float(tb["volume"].sum())],
            },
            index=pd.MultiIndex.from_tuples(
                [(ticker, tb.index[-1])], names=["ticker", "ts"]
            ),
        )
        frames.append(row)
    return pd.concat(frames).sort_index()


class Engine:
    def __init__(
        self,
        settings: Settings,
        store: BarStore,
        source: DataSource,
        ledger: SignalLedger,
        notifier: Notifier,
        enrichers: list[object] | None = None,   # Phase 2 UZI-Skill 钩子，暂不使用
    ) -> None:
        self.settings = settings
        self.store = store
        self.source = source
        self.ledger = ledger
        self.notifier = notifier
        self.enrichers = enrichers or []
        mp = settings.strategies["momentum_rotation"]
        bp = settings.strategies["breakout_20d"]
        self.momentum = MomentumRotation(
            universe=settings.universe,
            lookback_days=int(mp["lookback_days"]),
            top_n=int(mp["top_n"]),
            min_dollar_volume=float(mp["min_dollar_volume"]),
            ticker_currency=settings.international_tickers,
        )
        self._intl_source = YFinanceSource()
        self.breakout = Breakout20d(
            universe=settings.watchlist,
            high_lookback_days=int(bp["high_lookback_days"]),
            volume_multiplier=float(bp["volume_multiplier"]),
        )

    # ---- 内部工具 ----

    def _dedup(self, signals: list[Signal], now: datetime):  # type: ignore[no-untyped-def]
        cfg = self.settings.notify
        return apply_dedup(
            signals,
            now,
            self.ledger.last_push_by_key(now - timedelta(hours=cfg.dedup_hours)),
            self.ledger.pushed_count_since(now - timedelta(hours=1)),
            dedup_hours=cfg.dedup_hours,
            hourly_limit=cfg.hourly_limit,
        )

    def _refresh_daily(self, now: datetime) -> pd.DataFrame:
        tickers = sorted(set(self.settings.universe) | set(self.settings.watchlist))
        intl = [t for t in tickers if t in self.settings.international_tickers]
        primary = [t for t in tickers if t not in self.settings.international_tickers]
        start = (now - timedelta(days=10)).date()
        end = now.date() + timedelta(days=1)
        if primary:
            fresh = self.source.fetch_daily_bars(primary, start, end)
            self.store.write_daily_bars(fresh, source=self.settings.data_source)
        if intl:
            fresh_intl = self._intl_source.fetch_daily_bars(intl, start, end)
            self.store.write_daily_bars(fresh_intl, source="yfinance")
        return self.store.read_daily_bars(tickers, start=now - timedelta(days=400))

    def _refresh_fx_rates(self) -> None:
        """只为实际出现在 universe 里的国际标的查汇率，避免无谓的网络请求。"""
        currencies = {
            self.settings.international_tickers[t]
            for t in self.settings.universe
            if t in self.settings.international_tickers
        }
        if currencies:
            self.momentum.fx_rates = fetch_usd_rates(currencies)

    # ---- 调度入口 ----

    def run_premarket(self, now: datetime) -> None:
        bars = self._refresh_daily(now)
        self._refresh_fx_rates()
        targets = self.momentum.generate(bars)
        target_tickers = [s.ticker for s in targets]
        current = self.ledger.get_holdings(self.momentum.strategy_id)
        # 与 targets 用同一根 bar 的时间戳，保证同一次调仓的信号落在同一天
        as_of = targets[0].ts if targets else now

        sells = [
            Signal(
                ticker=t,
                direction=Direction.SELL,
                price=float(bars.xs(t, level="ticker")["close"].iloc[-1]),
                reason="动量排名跌出前列，轮动调出",
                strategy_id=self.momentum.strategy_id,
                ts=as_of,
            )
            for t in current
            if t not in target_tickers and t in bars.index.get_level_values("ticker")
        ]
        all_signals = targets + sells
        result = self._dedup(all_signals, now)
        for s in result.to_push:
            self.ledger.insert(s, pushed=True, now=now)
        for s in result.suppressed + result.overflow:
            self.ledger.insert(s, pushed=False, now=now)
        self.ledger.set_holdings(self.momentum.strategy_id, target_tickers)

        if result.to_push:
            lines = ["| 标的 | 方向 | 价格 | 原因 |", "|---|---|---|---|"]
            lines += [
                f"| {s.ticker} | {s.direction.value.upper()} | {s.price:.2f} | {s.reason} |"
                for s in result.to_push
            ]
            self.notifier.send(report_card("📋 盘前早报", "\n".join(lines)))
        log.info("premarket.done", signals=len(all_signals), pushed=len(result.to_push))

    def run_intraday(self, now: datetime) -> None:
        intraday = self.source.fetch_intraday_bars(self.settings.watchlist)
        self.store.write_intraday_bars(intraday, source=self.settings.data_source)
        daily = self.store.read_daily_bars(
            self.settings.watchlist, start=now - timedelta(days=60)
        )
        bars = _intraday_snapshot(daily, intraday, now.date())
        result = self._dedup(self.breakout.generate(bars), now)
        delayed = self.settings.data_source == "yfinance"
        for s in result.to_push:
            self.ledger.insert(s, pushed=True, now=now)
            self.notifier.send(signal_card(s, delayed=delayed))
        for s in result.suppressed:
            self.ledger.insert(s, pushed=False, now=now)
        if result.overflow:
            for s in result.overflow:
                self.ledger.insert(s, pushed=False, now=now)
            names = ", ".join(f"{s.ticker}({s.direction.value})" for s in result.overflow)
            self.notifier.send(
                alert_card(
                    "信号限流汇总",
                    f"1 小时配额已满，以下 {len(result.overflow)} 条合并：{names}",
                )
            )
        log.info(
            "intraday.done", pushed=len(result.to_push), overflow=len(result.overflow)
        )
