from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant_signal.strategies.base import Direction, Signal


@dataclass(frozen=True)
class TrendGateConfig:
    ma_days: int = 200
    mom_days: int = 252
    buffer: float = 0.03
    benchmark: str = "SPY"
    defensive: tuple[str, ...] = ("BIL", "TLT", "GLD")


@dataclass(frozen=True)
class TrendInfo:
    ticker: str
    state: str      # "LONG" | "FLAT"
    signal: str     # "ENTER" | "HOLD" | "EXIT" | "WARN"
    price: float
    sma200: float
    sell_ref: float
    ret_12m: float
    rf_12m: float


def _weekly_marks(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """每个自然周最后一个交易日（近似周五收盘检查点）。"""
    s = pd.Series(list(index), index=index)
    return list(s.groupby(index.tz_localize(None).to_period("W")).last())


def _replay(
    close: pd.Series,
    benchmark_close: pd.Series,
    rf_close: pd.Series,
    is_stock: bool,
    is_usd: bool,
    cfg: TrendGateConfig,
    use_ma: bool,
    use_mom: bool,
) -> list[tuple[pd.Timestamp, str, str, float, float, float, float]]:
    """一次遍历所有周五，产出每周 (ts, state, signal, price, sma, ret, rf)。无未来函数。"""
    if not is_usd:
        use_mom = False   # 非美元只用 200 线

    sma = close.rolling(cfg.ma_days).mean()
    ret = close / close.shift(cfg.mom_days) - 1.0
    bench_ret = (
        (benchmark_close / benchmark_close.shift(cfg.mom_days) - 1.0)
        .reindex(close.index)
        .ffill()
    )
    rf_ret = (rf_close / rf_close.shift(cfg.mom_days) - 1.0).reindex(close.index).ffill()

    records: list[tuple[pd.Timestamp, str, str, float, float, float, float]] = []
    state, signal = "FLAT", "HOLD"
    for ts in _weekly_marks(pd.DatetimeIndex(close.index)):
        s_val = sma.loc[ts]
        r_val = ret.loc[ts]
        if pd.isna(s_val) or pd.isna(r_val):
            continue
        price = float(close.loc[ts])
        s = float(s_val)
        r = float(r_val)
        rf = float(rf_ret.loc[ts]) if not pd.isna(rf_ret.loc[ts]) else 0.0

        cond_ma = price > s
        cond_break = price < s * (1 - cfg.buffer)
        cond_mom = r > rf
        b = float(bench_ret.loc[ts]) if not pd.isna(bench_ret.loc[ts]) else 0.0
        cond_rs = (r > b) if is_stock else True

        enter_terms = []
        if use_ma:
            enter_terms.append(cond_ma)
        if use_mom:
            enter_terms.append(cond_mom)
            enter_terms.append(cond_rs)
        enter = all(enter_terms) if enter_terms else False

        break_fail = cond_break if use_ma else False
        mom_fail = (not cond_mom) if use_mom else False
        do_exit = (break_fail and mom_fail) if (use_ma and use_mom) else (break_fail or mom_fail)
        warn = break_fail or mom_fail

        if state == "FLAT":
            signal = "ENTER" if enter else "HOLD"
            if enter:
                state = "LONG"
        else:  # LONG
            if do_exit:
                state, signal = "FLAT", "EXIT"
            elif warn:
                signal = "WARN"
            else:
                signal = "HOLD"
        records.append((ts, state, signal, price, s, r, rf))
    return records


def trend_state(
    ticker: str,
    close: pd.Series,
    benchmark_close: pd.Series,
    rf_close: pd.Series,
    is_stock: bool,
    is_usd: bool,
    cfg: TrendGateConfig,
    as_of: pd.Timestamp,
    use_ma: bool = True,
    use_mom: bool = True,
) -> TrendInfo | None:
    """逐周五重放迟滞状态机，推导 as_of 时的 state/signal/sell_ref。无未来函数。

    use_ma/use_mom 供回测对照组：仅关一个即得"仅200线"/"仅绝对动量"变体。
    非美元标的(is_usd=False)强制只用 cond_ma（跨币种不比 mom/rs）。
    """
    close = close.dropna().sort_index()
    close = close[close.index <= as_of]
    if len(close) < cfg.ma_days + 1:
        return None
    records = _replay(close, benchmark_close, rf_close, is_stock, is_usd, cfg, use_ma, use_mom)
    if not records:
        return None
    _, state, signal, price, s, r, rf = records[-1]
    return TrendInfo(
        ticker=ticker, state=state, signal=signal, price=price, sma200=s,
        sell_ref=s * (1 - cfg.buffer), ret_12m=r, rf_12m=rf,
    )


def weekly_state_map(
    close: pd.Series,
    benchmark_close: pd.Series,
    rf_close: pd.Series,
    is_stock: bool,
    is_usd: bool,
    cfg: TrendGateConfig,
    use_ma: bool = True,
    use_mom: bool = True,
) -> pd.Series:
    """整段历史每个周五的 state（"LONG"/"FLAT"），供回测一次性预计算。"""
    close = close.dropna().sort_index()
    if len(close) < cfg.ma_days + 1:
        return pd.Series(dtype=object)
    records = _replay(close, benchmark_close, rf_close, is_stock, is_usd, cfg, use_ma, use_mom)
    return pd.Series({ts: state for ts, state, *_ in records})


def _defensive_pick(
    close: pd.DataFrame, cfg: TrendGateConfig, as_of: pd.Timestamp, rf_close: pd.Series
) -> str | None:
    """{BIL,TLT,GLD} 里按 ret_12m − rf_12m 最强者。"""
    best, best_score = None, float("-inf")
    for t in cfg.defensive:
        if t not in close.columns:
            continue
        s = close[t].dropna()
        s = s[s.index <= as_of]
        if len(s) < cfg.mom_days + 1:
            continue
        ret = float(s.iloc[-1] / s.iloc[-1 - cfg.mom_days] - 1.0)
        rf = rf_close.reindex(s.index).ffill()
        rf12 = float(rf.iloc[-1] / rf.iloc[-1 - cfg.mom_days] - 1.0) if len(rf) > cfg.mom_days else 0.0
        score = ret - rf12
        if score > best_score:
            best, best_score = t, score
    return best


def apply_trend_gate(
    picks: list[Signal],
    bars: pd.DataFrame,
    asset_type: dict[str, str],
    international_tickers: dict[str, str],
    cfg: TrendGateConfig,
    as_of: pd.Timestamp | None = None,
    use_ma: bool = True,
    use_mom: bool = True,
) -> tuple[list[Signal], list[TrendInfo]]:
    """动量 picks 中趋势 LONG 的原样保留，FLAT 的释放权重合并投入单一最强防御标的。"""
    close = bars["close"].unstack("ticker").sort_index()
    if as_of is None:
        as_of = close.index[-1]
    bench = close[cfg.benchmark] if cfg.benchmark in close.columns else pd.Series(dtype=float)
    rf = close["BIL"] if "BIL" in close.columns else pd.Series(dtype=float)

    kept: list[Signal] = []
    infos: list[TrendInfo] = []
    freed = 0.0
    for p in picks:
        if p.ticker not in close.columns:
            kept.append(p)
            continue
        info = trend_state(
            p.ticker, close[p.ticker], bench, rf,
            is_stock=asset_type.get(p.ticker, "STOCK") == "STOCK",
            is_usd=p.ticker not in international_tickers,
            cfg=cfg, as_of=as_of, use_ma=use_ma, use_mom=use_mom,
        )
        if info is not None:
            infos.append(info)
        if info is None or info.state == "LONG":
            kept.append(p)
        else:
            freed += p.suggested_weight or 0.0

    if freed > 0:
        d = _defensive_pick(close, cfg, as_of, rf)
        if d is not None:
            kept.append(
                Signal(
                    ticker=d, direction=Direction.BUY,
                    price=float(close[d].dropna().iloc[-1]),
                    reason="趋势闸门·防御切换", strategy_id="momentum_rotation",
                    ts=as_of.to_pydatetime(), suggested_weight=round(freed, 4),
                )
            )
    return kept, infos
