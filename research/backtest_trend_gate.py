"""趋势闸门阶段B回测：证明"动量+趋势闸门+防御切换"能降最大回撤。

5组对照：基线动量 / 仅200线 / 仅绝对动量 / 完整闸门+防御 / buy&hold SPY。
buffer 扫描 {0.02,0.03,0.05}；月度退出 vs 周度退出对照。
复用 MomentumRotation.generate() 月末选股 + trend_gate 的 weekly_state_map 预计算逐周状态。
研究脚本，靠真实缓存数据跑通，不走 TDD。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.momentum_rotation import MomentumRotation
from quant_signal.strategies.trend_gate import (
    TrendGateConfig,
    _weekly_marks,
    weekly_state_map,
)

REPORTS = Path(__file__).parent / "reports"
START = date(2016, 1, 1)
EXTRA = ["BIL", "TLT", "GLD", "SPY"]   # 防御/基准/rf 所需的额外数据标的
_FLAGS = {"full": (True, True), "ma_only": (True, False), "mom_only": (False, True)}


def load_bars() -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = sorted(set(settings.universe) | set(EXTRA))
    missing = [t for t in tickers if store.daily_bar_count(t) < 100]
    if missing:
        df = YFinanceSource().fetch_daily_bars(missing, START, datetime.now(timezone.utc).date())
        store.write_daily_bars(df, source="yfinance")
    start_dt = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
    return store.read_daily_bars(tickers, start=start_dt)


def _defensive_series(close: pd.DataFrame, cfg: TrendGateConfig) -> pd.Series:
    """每日 {BIL,TLT,GLD} 里 ret_12m − rf_12m 最强者（列名）。"""
    if "BIL" not in close.columns:
        return pd.Series(index=close.index, dtype=object)
    rf12 = close["BIL"] / close["BIL"].shift(cfg.mom_days) - 1.0
    scores = {}
    for t in cfg.defensive:
        if t in close.columns:
            scores[t] = (close[t] / close[t].shift(cfg.mom_days) - 1.0) - rf12
    sdf = pd.DataFrame(scores).dropna(how="all")
    return sdf.idxmax(axis=1).reindex(close.index)


def _momentum(settings) -> MomentumRotation:  # type: ignore[no-untyped-def]
    mp = settings.strategies["momentum_rotation"]
    return MomentumRotation(
        universe=settings.universe, lookback_days=int(mp["lookback_days"]),
        top_n=int(mp["top_n"]), min_dollar_volume=float(mp["min_dollar_volume"]),
        ticker_currency=settings.international_tickers,
        group_top_n=settings.momentum_group_top_n, asset_type=settings.asset_type,
        default_group_top_n=settings.momentum_default_group_top_n,
    )


def _metrics(pf: vbt.Portfolio, close_index: pd.DatetimeIndex, changes: int) -> dict[str, float]:
    stats = pf.stats()
    years = (close_index[-1] - close_index[0]).days / 365.25
    total = float(stats["Total Return [%]"]) / 100
    return {
        "annual": ((1 + total) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "maxdd": float(stats["Max Drawdown [%]"]),
        "turnover": changes / years,
    }


def build_weights(bars: pd.DataFrame, settings, gate: str, buffer: float, weekly_exit: bool):  # type: ignore[no-untyped-def]
    """构建每日目标权重矩阵，返回 (weights, close, changes)。build_portfolio 与
    盈亏比脚本共用，保证评估的是同一个策略。"""
    close = bars["close"].unstack("ticker").sort_index()
    cfg = TrendGateConfig(buffer=buffer)
    mom = _momentum(settings)
    month_ends = set(close.groupby(close.index.tz_localize(None).to_period("M")).tail(1).index)
    exit_marks = set(_weekly_marks(pd.DatetimeIndex(close.index))) if weekly_exit else set(month_ends)

    daily_state: dict[str, pd.Series] = {}
    defensive_daily = pd.Series(index=close.index, dtype=object)
    if gate != "none":
        use_ma, use_mom = _FLAGS[gate]
        bench = close[cfg.benchmark]
        rf = close["BIL"]
        for t in settings.universe:
            if t not in close.columns:
                continue
            wsm = weekly_state_map(
                close[t], bench, rf,
                is_stock=settings.asset_type.get(t, "STOCK") == "STOCK",
                is_usd=t not in settings.international_tickers,
                cfg=cfg, use_ma=use_ma, use_mom=use_mom,
            )
            if not wsm.empty:
                daily_state[t] = wsm.reindex(close.index, method="ffill")
        defensive_daily = _defensive_series(close, cfg)

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    changes = 0
    prev: set[str] = set()
    current: list = []
    for ts in close.index:
        is_me = ts in month_ends
        if is_me:
            current = mom.generate(bars[bars.index.get_level_values("ts") <= ts])
        if (is_me or ts in exit_marks) and current:
            n = len(current)
            hold: dict[str, float] = {}
            freed = 0.0
            for s in current:
                w = s.suggested_weight or (1.0 / n)
                st = daily_state.get(s.ticker)
                state = str(st.loc[ts]) if (st is not None and pd.notna(st.loc[ts])) else "LONG"
                if gate == "none" or state == "LONG":
                    hold[s.ticker] = hold.get(s.ticker, 0.0) + w
                else:
                    freed += w
            if freed > 0:
                d = defensive_daily.loc[ts] if ts in defensive_daily.index else None
                if isinstance(d, str):
                    hold[d] = hold.get(d, 0.0) + freed
            if hold:
                weights.loc[ts:, :] = 0.0
                for t, w in hold.items():
                    weights.loc[ts:, t] = w
                names = set(hold)
                changes += len(names - prev)
                prev = names

    return weights, close, changes


def build_portfolio(bars: pd.DataFrame, settings, gate: str, buffer: float, weekly_exit: bool):  # type: ignore[no-untyped-def]
    """构建组合并返回 (pf, close_index, changes)。"""
    weights, close, changes = build_weights(bars, settings, gate, buffer, weekly_exit)
    pf = vbt.Portfolio.from_orders(
        close=close, size=weights, size_type="targetpercent", freq="1D",
        cash_sharing=True, call_seq="auto",
    )
    return pf, pd.DatetimeIndex(close.index), changes


def run_variant(bars: pd.DataFrame, settings, gate: str, buffer: float, weekly_exit: bool):  # type: ignore[no-untyped-def]
    pf, idx, changes = build_portfolio(bars, settings, gate, buffer, weekly_exit)
    return _metrics(pf, idx, changes)


def _spy_bh(bars: pd.DataFrame) -> dict[str, float]:
    close = bars["close"].unstack("ticker").sort_index()
    pf = vbt.Portfolio.from_holding(close["SPY"], freq="1D")
    return _metrics(pf, pd.DatetimeIndex(close.index), 0)


def _row(name: str, m: dict[str, float]) -> str:
    return f"| {name} | {m['annual']:.1f} | {m['sharpe']:.2f} | {m['maxdd']:.1f} | {m['turnover']:.1f} |"


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    print("回测标的:", sorted(bars.index.get_level_values("ticker").unique()))

    hdr = "| 方案 | 年化% | Sharpe | 最大回撤% | 年换手 |\n|---|---|---|---|---|"

    # 主对照（buffer=0.03，周度退出）
    main_rows = [hdr]
    for gate, label in [("none", "基线(无闸门)"), ("ma_only", "仅200线"),
                        ("mom_only", "仅绝对动量"), ("full", "完整闸门+防御")]:
        m = run_variant(bars, settings, gate, 0.03, weekly_exit=True)
        main_rows.append(_row(label, m))
        print("done main", gate)
    main_rows.append(_row("buy&hold SPY", _spy_bh(bars)))

    # buffer 扫描（full，周度退出）
    buf_rows = [hdr]
    for b in (0.02, 0.03, 0.05):
        m = run_variant(bars, settings, "full", b, weekly_exit=True)
        buf_rows.append(_row(f"full buffer={b}", m))
        print("done buffer", b)

    # 月度 vs 周度退出（full，buffer=0.03）
    freq_rows = [hdr]
    for wk, label in [(False, "full 月度退出"), (True, "full 周度退出")]:
        m = run_variant(bars, settings, "full", 0.03, weekly_exit=wk)
        freq_rows.append(_row(label, m))
        print("done freq", label)

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"trend_gate_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 趋势闸门 阶段B 回测报告\n\n"
        f"- 区间: {START} 至今，universe={settings.universe}\n"
        "- 月末动量选股 + 每周五评估趋势闸门退出，FLAT 仓位切最强防御(BIL/TLT/GLD)\n"
        "- 无风险利率基准 BIL；个股相对强弱基准 SPY；非美元标的(港股/韩股)只用200日线\n\n"
        "## 主对照（buffer=0.03，周度退出）\n\n" + "\n".join(main_rows) + "\n\n"
        "## buffer 扫描（完整闸门，周度退出）\n\n" + "\n".join(buf_rows) + "\n\n"
        "## 退出频率对照（完整闸门，buffer=0.03）\n\n" + "\n".join(freq_rows) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
