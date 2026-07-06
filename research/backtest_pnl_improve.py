"""盈亏比优化对照回测：在上线配置(动量+仅200线闸门+防御)上叠加两种改法。

对照组：
  base      : 上线配置(仅200线+防御, 等权)         —— 复刻校验，应与主回测一致
  +chand    : 叠加 ATR 吊灯日度止损(收盘跌破 22日高−3×ATR 即清仓到现金)
  +voltgt   : 叠加反波动率仓位(权重 ∝ 1/波动率, 20日)
  +both     : 两者叠加

每组同时报组合指标(年化/夏普/最大回撤)与 per-trade 盈亏比(胜率/payoff/盈利因子/期望值)，
看这两种改法到底是净改善，还是"砍尾也砍了赢家"。
复用 backtest_trend_gate 与 backtest_pnl_ratio 的构件，口径与主回测/卡片一致。
研究脚本，靠真实缓存数据跑通。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

sys.path.insert(0, str(Path(__file__).parent))

from backtest_pnl_ratio import round_trips, trade_metrics  # noqa: E402
from backtest_trend_gate import (  # noqa: E402
    REPORTS,
    START,
    _defensive_series,
    _metrics,
    _momentum,
    load_bars,
)

from quant_signal.config import load_settings  # noqa: E402
from quant_signal.strategies.trend_gate import (  # noqa: E402
    TrendGateConfig,
    _weekly_marks,
    weekly_state_map,
)

DEFENSIVE = {"BIL", "TLT", "GLD"}
# 吊灯止损参数与实盘卡片(config.trend_gate)一致
CHAND_LOOKBACK, CHAND_ATR, CHAND_MULT = 22, 14, 3.0
VOL_LOOKBACK = 20


def _chandelier_series(high: pd.Series, low: pd.Series, close: pd.Series, mult: float) -> pd.Series:
    """向量化吊灯止损：近 lookback 日最高价 − mult×ATR，与 indicators.chandelier_stop 同口径。"""
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(CHAND_ATR).mean()
    return high.rolling(CHAND_LOOKBACK).max() - mult * atr


def build_weights_ex(bars: pd.DataFrame, settings, use_stop: bool, use_voltarget: bool, chand_mult: float = CHAND_MULT):  # type: ignore[no-untyped-def]
    """在 ma_only+防御 基础上，可选叠加吊灯止损/反波动率仓位，返回 (weights, close, changes)。"""
    close = bars["close"].unstack("ticker").sort_index()
    high = bars["high"].unstack("ticker").sort_index().reindex_like(close)
    low = bars["low"].unstack("ticker").sort_index().reindex_like(close)
    cfg = TrendGateConfig(buffer=0.03)
    mom = _momentum(settings)

    month_ends = set(close.groupby(close.index.tz_localize(None).to_period("M")).tail(1).index)
    exit_marks = set(_weekly_marks(pd.DatetimeIndex(close.index)))

    # ma_only 趋势状态(每票逐周, ffill 到每日)
    daily_state: dict[str, pd.Series] = {}
    bench, rf = close[cfg.benchmark], close["BIL"]
    for t in settings.universe:
        if t not in close.columns:
            continue
        wsm = weekly_state_map(
            close[t], bench, rf,
            is_stock=settings.asset_type.get(t, "STOCK") == "STOCK",
            is_usd=t not in settings.international_tickers,
            cfg=cfg, use_ma=True, use_mom=False,
        )
        if not wsm.empty:
            daily_state[t] = wsm.reindex(close.index, method="ffill")
    defensive_daily = _defensive_series(close, cfg)

    stop = {t: _chandelier_series(high[t], low[t], close[t], chand_mult) for t in close.columns} if use_stop else {}
    vol = close.pct_change(fill_method=None).rolling(VOL_LOOKBACK).std() if use_voltarget else None

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    changes = 0
    prev: set[str] = set()
    held: dict[str, float] = {}
    current: list = []
    for ts in close.index:
        is_me = ts in month_ends
        if is_me:
            current = mom.generate(bars[bars.index.get_level_values("ts") <= ts])
        if (is_me or ts in exit_marks) and current:
            n = len(current)
            # 反波动率原始权重(整体归一到1)，否则沿用 suggested_weight or 1/n
            raw: dict[str, float] = {}
            for s in current:
                if use_voltarget and vol is not None:
                    v = vol[s.ticker].loc[ts] if s.ticker in vol.columns else None
                    raw[s.ticker] = (1.0 / float(v)) if (v is not None and pd.notna(v) and v > 0) else None  # type: ignore[assignment]
                else:
                    raw[s.ticker] = s.suggested_weight or (1.0 / n)
            if use_voltarget:
                filled = [x for x in raw.values() if x is not None]
                fallback = (sum(filled) / len(filled)) if filled else 1.0
                raw = {k: (v if v is not None else fallback) for k, v in raw.items()}
                tot = sum(raw.values())
                raw = {k: v / tot for k, v in raw.items()}
            hold: dict[str, float] = {}
            freed = 0.0
            for s in current:
                st = daily_state.get(s.ticker)
                state = str(st.loc[ts]) if (st is not None and pd.notna(st.loc[ts])) else "LONG"
                if state == "LONG":
                    hold[s.ticker] = hold.get(s.ticker, 0.0) + raw[s.ticker]
                else:
                    freed += raw[s.ticker]
            if freed > 0:
                d = defensive_daily.loc[ts] if ts in defensive_daily.index else None
                if isinstance(d, str):
                    hold[d] = hold.get(d, 0.0) + freed
            held = hold  # 调仓日重建目标
        # 吊灯日度止损：核心腿(非防御)收盘跌破止损即清仓到现金
        if use_stop and held:
            for t in list(held):
                if t in DEFENSIVE:
                    continue
                s_lvl = stop[t].loc[ts] if t in stop else None
                c = close[t].loc[ts]
                if s_lvl is not None and pd.notna(s_lvl) and pd.notna(c) and c < s_lvl:
                    del held[t]
        if held:
            weights.loc[ts, list(held.keys())] = list(held.values())
            names = set(held)
            changes += len(names - prev)
            prev = names
    return weights, close, changes


def _portfolio(weights: pd.DataFrame, close: pd.DataFrame):  # type: ignore[no-untyped-def]
    return vbt.Portfolio.from_orders(
        close=close, size=weights, size_type="targetpercent", freq="1D",
        cash_sharing=True, call_seq="auto",
    )


VARIANTS = [
    ("base", False, False, "上线配置(仅200线+防御)"),
    ("chand", True, False, "+吊灯日度止损"),
    ("voltgt", False, True, "+反波动率仓位"),
    ("both", True, True, "+吊灯+反波动率"),
]


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    print("回测标的:", sorted(bars.index.get_level_values("ticker").unique()))

    rows = []
    for key, use_stop, use_vt, label in VARIANTS:
        weights, close, changes = build_weights_ex(bars, settings, use_stop, use_vt)
        pf = _portfolio(weights, close)
        pm = _metrics(pf, pd.DatetimeIndex(close.index), changes)
        rets, open_n = round_trips(weights, close)
        tm = trade_metrics(rets, open_n)
        print(f"\n=== {label} ===  年化{pm['annual']:.1f} 夏普{pm['sharpe']:.2f} "
              f"回撤{pm['maxdd']:.1f} | 交易{tm['trades']:.0f} 胜率{tm['win_rate']:.1f} "
              f"payoff{tm['payoff']:.2f} 盈利因子{tm['profit_factor']:.2f} 期望{tm['expectancy']:.2f}")
        rows.append((label, pm, tm))

    hdr = ("| 方案 | 年化% | 夏普 | 最大回撤% | 年换手 | 交易 | 胜率% | 平均盈% | 平均亏% | "
           "盈亏比 | 盈利因子 | 期望%/笔 | 最差% |")
    sep = "|" + "---|" * 13
    lines = [hdr, sep]
    for label, pm, tm in rows:
        lines.append(
            f"| {label} | {pm['annual']:.1f} | {pm['sharpe']:.2f} | {pm['maxdd']:.1f} | "
            f"{pm['turnover']:.1f} | {tm['trades']:.0f} | {tm['win_rate']:.1f} | {tm['avg_win']:.1f} | "
            f"{tm['avg_loss']:.1f} | {tm['payoff']:.2f} | {tm['profit_factor']:.2f} | "
            f"{tm['expectancy']:.2f} | {tm['worst']:.1f} |"
        )

    # 吊灯倍数扫描（看放宽止损能否救回 A1；voltarget 关）
    sweep = [hdr, sep]
    print("\n--- 吊灯倍数扫描 ---")
    for mult in (4.0, 5.0, 6.0):
        weights, close, changes = build_weights_ex(bars, settings, True, False, chand_mult=mult)
        pf = _portfolio(weights, close)
        pm = _metrics(pf, pd.DatetimeIndex(close.index), changes)
        rets, open_n = round_trips(weights, close)
        tm = trade_metrics(rets, open_n)
        print(f"吊灯{mult:.0f}×ATR: 年化{pm['annual']:.1f} 夏普{pm['sharpe']:.2f} "
              f"回撤{pm['maxdd']:.1f} payoff{tm['payoff']:.2f} 期望{tm['expectancy']:.2f}")
        sweep.append(
            f"| +吊灯{mult:.0f}×ATR | {pm['annual']:.1f} | {pm['sharpe']:.2f} | {pm['maxdd']:.1f} | "
            f"{pm['turnover']:.1f} | {tm['trades']:.0f} | {tm['win_rate']:.1f} | {tm['avg_win']:.1f} | "
            f"{tm['avg_loss']:.1f} | {tm['payoff']:.2f} | {tm['profit_factor']:.2f} | "
            f"{tm['expectancy']:.2f} | {tm['worst']:.1f} |"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"pnl_improve_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 盈亏比优化对照回测\n\n"
        f"- 区间: {START} 至今，universe={settings.universe}\n"
        "- 基准=上线配置(月末动量选股+周五200线闸门+FLAT切防御, 等权)\n"
        f"- +吊灯: 核心腿收盘跌破 近{CHAND_LOOKBACK}日高−{CHAND_MULT}×ATR({CHAND_ATR}) 即清仓到现金(日度检查)\n"
        f"- +反波动率: 权重∝1/波动率({VOL_LOOKBACK}日收益std)，整体归一\n"
        "- 组合指标走 vectorbt；盈亏比走连续持有段 close-to-close 往返\n\n"
        "## 主对照\n\n" + "\n".join(lines) + "\n\n"
        "## 吊灯倍数扫描（反波动率关）\n\n" + "\n".join(sweep) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
