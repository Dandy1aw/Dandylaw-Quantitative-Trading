"""研究回测共享的可实现撮合口径。"""

from __future__ import annotations

import pandas as pd
import vectorbt as vbt


def next_bar_targets(
    signal_weights: pd.DataFrame, open_prices: pd.DataFrame
) -> pd.DataFrame:
    """把收盘后得到的目标权重延后一根 bar；无开盘价时不发单。"""
    aligned_open = open_prices.reindex_like(signal_weights)
    targets = signal_weights.shift(1).fillna(0.0)
    return targets.where(aligned_open.notna())


def build_next_open_portfolio(
    close: pd.DataFrame,
    open_prices: pd.DataFrame,
    signal_weights: pd.DataFrame,
    *,
    fees: float = 0.0005,
    slippage: float = 0.0005,
) -> vbt.Portfolio:
    """按下一根日线开盘撮合，收盘价用于组合估值。"""
    targets = next_bar_targets(signal_weights, open_prices)
    return vbt.Portfolio.from_orders(
        close=close,
        price=open_prices.reindex_like(close),
        size=targets.reindex_like(close),
        size_type="targetpercent",
        fees=fees,
        slippage=slippage,
        freq="1D",
        cash_sharing=True,
        call_seq="auto",
    )
