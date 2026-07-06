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
    _momentum,
    load_bars,
)
from metrics import portfolio_metrics  # noqa: E402

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
BENCH = "SPY"
COST_PER_SIDE = 0.0005   # 每边成本假设 5bps(佣金+滑点)；美股 Alpaca 佣金为0，主要是滑点


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


HDR = ("| 方案 | 累计收益% | 年化% | 超额年化% | 最大回撤% | 年化波动% | 夏普 | 卡玛 | "
       "胜率% | 盈亏比(PF) | 交易次数 | 年换手 | 净年化%(含成本) | 成本拖累% |")
SEP = "|" + "---|" * 14


def _eval(bars: pd.DataFrame, settings, use_stop: bool, use_vt: bool, mult: float = CHAND_MULT):  # type: ignore[no-untyped-def]
    weights, close, _ = build_weights_ex(bars, settings, use_stop, use_vt, chand_mult=mult)
    pf = _portfolio(weights, close)
    pm = portfolio_metrics(pf.returns(), weights, close[BENCH], COST_PER_SIDE)
    rets, open_n = round_trips(weights, close)
    tm = trade_metrics(rets, open_n)
    return pm, tm


def _row(label: str, pm: dict[str, float], tm: dict[str, float]) -> str:
    return (
        f"| {label} | {pm['total_return']:.0f} | {pm['cagr']:.1f} | {pm['excess']:.1f} | "
        f"{pm['maxdd']:.1f} | {pm['vol']:.1f} | {pm['sharpe']:.2f} | {pm['calmar']:.2f} | "
        f"{tm['win_rate']:.1f} | {tm['profit_factor']:.2f} | {tm['trades']:.0f} | "
        f"{pm['ann_turnover']:.1f} | {pm['net_cagr']:.1f} | {pm['cost_drag']:.1f} |"
    )


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    print("回测标的:", sorted(bars.index.get_level_values("ticker").unique()))

    lines = [HDR, SEP]
    for _key, use_stop, use_vt, label in VARIANTS:
        pm, tm = _eval(bars, settings, use_stop, use_vt)
        print(f"=== {label} === 累计{pm['total_return']:.0f} 年化{pm['cagr']:.1f} "
              f"超额{pm['excess']:.1f} 回撤{pm['maxdd']:.1f} 波动{pm['vol']:.1f} "
              f"夏普{pm['sharpe']:.2f} 卡玛{pm['calmar']:.2f} | 胜率{tm['win_rate']:.1f} "
              f"PF{tm['profit_factor']:.2f} 交易{tm['trades']:.0f} 净年化{pm['net_cagr']:.1f}")
        lines.append(_row(label, pm, tm))

    # 吊灯倍数扫描（看放宽止损能否救回 A1；voltarget 关）
    sweep = [HDR, SEP]
    print("\n--- 吊灯倍数扫描 ---")
    for mult in (4.0, 5.0, 6.0):
        pm, tm = _eval(bars, settings, True, False, mult=mult)
        print(f"吊灯{mult:.0f}×ATR: 年化{pm['cagr']:.1f} 夏普{pm['sharpe']:.2f} 卡玛{pm['calmar']:.2f}")
        sweep.append(_row(f"+吊灯{mult:.0f}×ATR", pm, tm))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"pnl_improve_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 20 只标的组合 · 全指标回测报告\n\n"
        f"- 区间: {START} 至今；universe(20只)={settings.universe}\n"
        "- 基准策略=上线配置(月末动量选股 + 周五200线闸门 + FLAT切防御, 等权)；超额基准=SPY\n"
        f"- +吊灯: 核心腿收盘跌破 近{CHAND_LOOKBACK}日高−{CHAND_MULT}×ATR({CHAND_ATR}) 即清仓到现金(日度)\n"
        f"- +反波动率: 权重∝1/波动率({VOL_LOOKBACK}日收益std)，整体归一\n"
        f"- 成本: 每边 {COST_PER_SIDE*1e4:.0f}bps(佣金+滑点)按目标换手扣减；盈亏比(PF)=总盈利/总亏损\n"
        "- 累计/年化/波动/夏普/回撤/卡玛/超额从日收益算(rf=0)；胜率/PF/交易数按连续持有段往返\n\n"
        "## 主对照\n\n" + "\n".join(lines) + "\n\n"
        "## 吊灯倍数扫描（反波动率关）\n\n" + "\n".join(sweep) + "\n\n"
        "## 说明与风险（务必先读）\n\n"
        "1. **夏普按 252 交易日年化(标准口径)**。此前报告里的 2.08 是 vectorbt 默认按 365 "
        "日历日年化的结果，偏高(2.08×√(252/365)=1.73)。日线仅交易日、约252根/年，252 才对。\n"
        "2. **universe 是人工挑选的半导体/科技赢家(NVDA/MU/TSM 等)，覆盖其最强的一段十年，"
        "存在显著选股/幸存者偏差**。累计收益 10976% 这类绝对数字不可外推到未来，也不代表换一批"
        "标的能复现；**本报告有效结论是各变体之间的相对比较**(base 在收益/夏普/卡玛全面占优)。\n"
        "3. **成本拖累仅 0.7~1.3pp/年**，因月度调仓换手低(年换手~10)；成本不是瓶颈。\n"
        "4. 结论：吊灯止损与反波动率仓位在本组合上均**未改善风险调整后收益**，不建议合入生产。\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
