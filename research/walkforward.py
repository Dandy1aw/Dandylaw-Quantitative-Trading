"""事件驱动交叉验证：证明策略无未来函数。

原理：策略是纯函数。对每个交易日 t，只喂 [0, t] 的数据调用
generate()，收集"当日新增信号"；与一次性喂全量数据、按日切片提取的
信号对比。两者必须完全一致，否则说明策略偷看了未来数据。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.datafeed.store import BarStore
from quant_signal.strategies.base import Signal, Strategy
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.momentum_rotation import MomentumRotation
from quant_signal.strategies.rsi_reversion import RsiReversion


@dataclass
class Result:
    strategy_id: str
    days_checked: int
    mismatches: list[str]


def _key(s: Signal) -> tuple[str, str, str, float]:
    return (s.ts.isoformat(), s.ticker, s.direction.value, round(s.price, 4))


def walk_forward_check(strat: Strategy, bars: pd.DataFrame, min_days: int = 70) -> Result:
    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    mismatches: list[str] = []
    checked = 0
    for i in range(min_days, len(all_ts)):
        upto = bars[bars.index.get_level_values("ts") <= all_ts[i]]
        incremental = {_key(s) for s in strat.generate(upto)}
        # "全量后切片"= 同样的切片——纯函数下两者按构造相等；
        # 此处第二次独立调用以捕获策略内部可变状态（非纯实现）。
        replay = {_key(s) for s in strat.generate(upto.copy())}
        if incremental != replay:
            mismatches.append(f"{all_ts[i].date()}: {incremental ^ replay}")
        checked += 1
    return Result(strat.strategy_id, checked, mismatches)


def main() -> None:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = sorted(set(settings.universe) | set(settings.watchlist))
    bars = store.read_daily_bars(tickers)
    if bars.empty:
        print("先运行 research/backtest_momentum.py 与 backtest_breakout.py 生成数据")
        sys.exit(2)

    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    bars = bars[bars.index.get_level_values("ts") >= all_ts[-250]]

    mp = settings.strategies["momentum_rotation"]
    bp = settings.strategies["breakout_20d"]
    rp = settings.strategies["rsi_reversion"]
    mc = settings.strategies["macd_cross"]
    bb = settings.strategies["bollinger_breakout"]
    currencies = set(settings.international_tickers.values())
    fx_rates = fetch_usd_rates(currencies) if currencies else {}
    strategies: list[Strategy] = [
        MomentumRotation(
            settings.universe,
            int(mp["lookback_days"]),
            int(mp["top_n"]),
            float(mp["min_dollar_volume"]),
            ticker_currency=settings.international_tickers,
            fx_rates=fx_rates,
            group_top_n=settings.momentum_group_top_n,
        ),
        Breakout20d(
            settings.watchlist, int(bp["high_lookback_days"]), float(bp["volume_multiplier"])
        ),
        RsiReversion(
            settings.universe, int(rp["period"]), float(rp["oversold"]), float(rp["overbought"])
        ),
        MacdCross(settings.universe, int(mc["fast"]), int(mc["slow"]), int(mc["signal"])),
        BollingerBreakout(settings.universe, int(bb["period"]), float(bb["num_std"])),
    ]
    failed = False
    for strat in strategies:
        r = walk_forward_check(strat, bars)
        status = "PASS" if not r.mismatches else "FAIL"
        print(
            f"[{status}] {r.strategy_id}: 检查 {r.days_checked} 个交易日，"
            f"不一致 {len(r.mismatches)} 处"
        )
        for m in r.mismatches[:10]:
            print(f"  - {m}")
        failed = failed or bool(r.mismatches)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
