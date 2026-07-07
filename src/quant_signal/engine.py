from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import structlog

from quant_signal.config import Settings
from quant_signal.datafeed.base import DataSource
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.enrichment import run_uzi_analysis
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Notifier
from quant_signal.notifier.cards import (
    alert_card,
    build_enrichment_card,
    momentum_ranking_card,
    premarket_cards,
    signal_card,
)
from quant_signal.notifier.dedup import apply_dedup
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.momentum_rotation import MomentumRotation
from quant_signal.strategies.rsi_reversion import RsiReversion
from quant_signal.strategies.indicators import chandelier_stop, expected_move_target
from quant_signal.strategies.trend_gate import TrendGateConfig, TrendInfo, apply_trend_gate
from quant_signal.watch_monitor import check_deviations

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
            group_top_n=settings.momentum_group_top_n,
            asset_type=settings.asset_type,
            default_group_top_n=settings.momentum_default_group_top_n,
        )
        tg = settings.trend_gate
        self.trend_gate_cfg = (
            TrendGateConfig(
                ma_days=tg.ma_days, mom_days=tg.mom_days, buffer=tg.buffer,
                benchmark=tg.benchmark, defensive=tuple(tg.defensive),
            )
            if tg.enabled else None
        )
        self.trend_gate_use_mom = tg.use_mom
        self._intl_source = YFinanceSource()
        self.breakout = Breakout20d(
            universe=settings.watchlist,
            high_lookback_days=int(bp["high_lookback_days"]),
            volume_multiplier=float(bp["volume_multiplier"]),
        )
        rp = settings.strategies.get("rsi_reversion", {})
        mc = settings.strategies.get("macd_cross", {})
        bb = settings.strategies.get("bollinger_breakout", {})
        self.rsi = RsiReversion(
            universe=settings.universe,
            period=int(rp.get("period", 14)),
            oversold=float(rp.get("oversold", 30)),
            overbought=float(rp.get("overbought", 70)),
        )
        self.macd = MacdCross(
            universe=settings.universe,
            fast=int(mc.get("fast", 12)),
            slow=int(mc.get("slow", 26)),
            signal=int(mc.get("signal", 9)),
        )
        self.bollinger = BollingerBreakout(
            universe=settings.universe,
            period=int(bb.get("period", 20)),
            num_std=float(bb.get("num_std", 2.0)),
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
        base = set(self.settings.universe) | set(self.settings.watchlist)
        if self.trend_gate_cfg is not None:
            # 趋势闸门需要 benchmark(SPY) 与防御 sleeve(BIL/TLT/GLD)的日线，BIL 非
            # universe 成员，需一并抓取（只取数、不参与动量选股）
            base |= {self.trend_gate_cfg.benchmark} | set(self.trend_gate_cfg.defensive)
        tickers = sorted(base)
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

    def _attach_exit_prices(self, signals: list[Signal], bars: pd.DataFrame) -> list[Signal]:
        """给 BUY 标的附止盈目标(波动率期望位)+止损价(ATR吊灯)；SELL 不附(卖出建议不给价)。"""
        tg = self.settings.trend_gate
        out: list[Signal] = []
        for s in signals:
            if s.direction != Direction.BUY:
                out.append(s)
                continue
            try:
                sub = bars.xs(s.ticker, level="ticker").sort_index()
            except KeyError:
                out.append(s)
                continue
            sl = chandelier_stop(
                sub["high"], sub["low"], sub["close"],
                lookback=tg.chandelier_lookback, atr_period=tg.chandelier_atr,
                mult=tg.chandelier_mult,
            )
            tp = expected_move_target(sub["close"], tg.tp_vol_lookback, tg.tp_horizon)
            extra = dict(s.extra or {})
            if sl is not None:
                extra["stop_loss"] = round(sl, 2)
            if tp is not None:
                extra["take_profit"] = round(tp, 2)
            out.append(replace(s, extra=extra) if extra != (s.extra or {}) else s)
        return out

    def _fetch_live_price(self, ticker: str) -> float | None:
        """尽力而为抓取含盘前/盘后的最新价作为'现价'展示；失败不影响主流程。
        统一走 yfinance(prepost=True)：Alpaca 免费 IEX 源无盘前/盘后，改用 yf 让
        美股盘前盘后也能刷新现价。信号链路仍走各自数据源(Alpaca/yf)，不受此影响。"""
        try:
            return self._intl_source.fetch_live_price(ticker)
        except Exception as e:  # noqa: BLE001
            log.warning("live_price.fetch_failed", ticker=ticker, error=str(e))
            return None

    # ---- 调度入口 ----

    def run_premarket(self, now: datetime) -> None:
        bars = self._refresh_daily(now)
        self._refresh_fx_rates()
        ranking = self.momentum.rank(bars)
        targets = self.momentum.generate(bars)
        trend_infos: list[TrendInfo] = []
        if self.trend_gate_cfg is not None and targets:
            # 动量选出后叠趋势闸门：趋势失效(跌破200线)的仓位切防御 sleeve
            gated, trend_infos = apply_trend_gate(
                targets, bars, self.settings.asset_type,
                self.settings.international_tickers, self.trend_gate_cfg,
                use_mom=self.trend_gate_use_mom,
            )
            targets = gated
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
        extra_signals = (
            self.rsi.generate(bars) + self.macd.generate(bars) + self.bollinger.generate(bars)
        )
        all_signals = self._attach_exit_prices(targets + sells + extra_signals, bars)
        result = self._dedup(all_signals, now)
        for s in result.to_push:
            self.ledger.insert(s, pushed=True, now=now)
        for s in result.suppressed + result.overflow:
            self.ledger.insert(s, pushed=False, now=now)
        self.ledger.set_holdings(self.momentum.strategy_id, target_tickers)

        if result.to_push:
            unique = {s.ticker for s in result.to_push}
            live_prices = {t: self._fetch_live_price(t) for t in unique}
            for card in premarket_cards(
                result.to_push, self.settings.international_tickers, live_prices
            ):
                self.notifier.send(card)
        self.notifier.send(
            momentum_ranking_card(
                ranking,
                held=set(current),
                trend_flat={i.ticker for i in trend_infos if i.state == "FLAT"},
                insufficient={i.ticker for i in trend_infos if i.state == "INSUFFICIENT"},
            )
        )
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

    def run_watch_deviation(self, now: datetime) -> None:
        """盘中5分钟检查：当前持仓(=今日目标)相对当天参考价偏离超阈值即提醒。"""
        watch_set = self.ledger.get_holdings(self.momentum.strategy_id)
        if not watch_set:
            log.info("watch_deviation.skip", reason="no_holdings")
            return

        since = now - timedelta(hours=24)
        ref_prices = {
            t: p
            for t in watch_set
            if (p := self.ledger.latest_signal_price(self.momentum.strategy_id, t, since)) is not None
        }
        if not ref_prices:
            log.info("watch_deviation.skip", reason="no_reference_price")
            return

        live_prices = {
            t: p for t in ref_prices if (p := self._fetch_live_price(t)) is not None
        }
        threshold = float(
            self.settings.strategies.get("price_deviation", {}).get("threshold", 0.02)
        )
        # 升级告警档位：入场阈值 ×(1, 2.5, 5, 10)，如 2%→[2%,5%,10%,20%]。破到更高档
        # 会再推一次，避免只在首次破 2% 时提醒、之后 4h 静默错过行情升级。
        bands = [threshold * m for m in (1.0, 2.5, 5.0, 10.0)]
        signals = check_deviations(ref_prices, live_prices, now=now, bands=bands)
        result = self._dedup(signals, now)
        for s in result.to_push:
            self.ledger.insert(s, pushed=True, now=now)
            self.notifier.send(signal_card(s))
        for s in result.suppressed + result.overflow:
            self.ledger.insert(s, pushed=False, now=now)
        log.info(
            "watch_deviation.done", checked=len(ref_prices), pushed=len(result.to_push)
        )

    def run_enrichment(self, now: datetime) -> None:
        """UZI-Skill 深度分析信息增强：持仓+今日全部策略BUY信号，headless跑一遍
        外部深度分析工具作为补充参考。enabled=false 时直接跳过。"""
        cfg = self.settings.enrichment
        if not cfg.enabled:
            return

        held = set(self.ledger.get_holdings(self.momentum.strategy_id))
        today_buys = {
            str(r["ticker"]) for r in self.ledger.signals_on(now.date()) if r["direction"] == "buy"
        }
        # UZI 只支持美股，港股/韩股会超时或产不出结果——直接跳过，省掉那段空等
        intl = set(self.settings.international_tickers)
        watch_set = sorted((held | today_buys) - intl)[: cfg.max_tickers]
        if not watch_set:
            log.info("enrichment.skip", reason="empty_watch_set")
            return

        results = []
        for ticker in watch_set:
            r = run_uzi_analysis(
                ticker, Path(cfg.uzi_run_py), cfg.python_exe, cfg.depth, cfg.timeout_seconds
            )
            if r is not None:
                results.append(r)
        if not results:
            log.info("enrichment.skip", reason="no_results", attempted=len(watch_set))
            return

        self.notifier.send(build_enrichment_card(results, held))
        log.info("enrichment.done", attempted=len(watch_set), succeeded=len(results))
