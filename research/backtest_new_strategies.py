"""RSI回归/MACD金叉死叉/布林带突破 三个新策略的回测（日线事件驱动）。

逐日截取 bars 喂 strategy.generate（与实盘同一份策略代码），收集 BUY/SELL
事件，统计信号后 5/10/20 日的价格表现。这三个策略是无状态的事件信号
（不像动量轮动那样管理组合持仓），用事件驱动而非月末调仓的评估方式。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.base import Direction, Strategy
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.rsi_reversion import RsiReversion

REPORTS = Path(__file__).parent / "reports"
HORIZONS = (5, 10, 20)


def load_bars(start: date) -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = settings.universe
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(
            tickers, start, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    # 缓存里可能有比 start 更早的历史（比如之前跑过全量回测），必须显式按
    # start 过滤，否则 --start 只在首次拉数据时有效，缓存命中后就是空操作。
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return store.read_daily_bars(tickers, start=start_dt)


def collect_events(
    strat: Strategy, bars: pd.DataFrame, min_days: int = 60
) -> list[tuple[pd.Timestamp, str, float, Direction]]:
    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    events: list[tuple[pd.Timestamp, str, float, Direction]] = []
    for i in range(min_days, len(all_ts)):
        window = bars[bars.index.get_level_values("ts") <= all_ts[i]]
        for s in strat.generate(window):
            events.append((all_ts[i], s.ticker, s.price, s.direction))
    return events


def evaluate(
    events: list[tuple[pd.Timestamp, str, float, Direction]],
    close: pd.DataFrame,
    direction: Direction,
) -> list[str]:
    all_ts = close.index
    rows = []
    for h in HORIZONS:
        rets: list[float] = []
        for ts, ticker, price, d in events:
            if d != direction or ticker not in close.columns:
                continue
            pos = all_ts.get_loc(ts)
            if pos + h < len(all_ts):
                exit_px = float(close[ticker].iloc[pos + h])
                ret = exit_px / price - 1.0
                # SELL 信号的"预测正确"是后续下跌，判胜负/均值时取反向收益
                rets.append(ret if direction == Direction.BUY else -ret)
        if rets:
            sr = pd.Series(rets)
            rows.append(f"| {h}日 | {len(sr)} | {(sr > 0).mean():.0%} | {sr.mean():+.2%} |")
        else:
            rows.append(f"| {h}日 | 0 | - | - |")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    settings = load_settings()
    bars = load_bars(args.start)
    close = bars["close"].unstack("ticker").sort_index()

    rp = settings.strategies["rsi_reversion"]
    mc = settings.strategies["macd_cross"]
    bb = settings.strategies["bollinger_breakout"]

    strategies: dict[str, Strategy] = {
        "RSI回归": RsiReversion(
            settings.universe, int(rp["period"]), float(rp["oversold"]), float(rp["overbought"])
        ),
        "MACD金叉死叉": MacdCross(
            settings.universe, int(mc["fast"]), int(mc["slow"]), int(mc["signal"])
        ),
        "布林带突破": BollingerBreakout(
            settings.universe, int(bb["period"]), float(bb["num_std"])
        ),
    }

    sections = []
    for name, strat in strategies.items():
        events = collect_events(strat, bars)
        buy_events = [e for e in events if e[3] == Direction.BUY]
        sell_events = [e for e in events if e[3] == Direction.SELL]
        buy_rows = evaluate(events, close, Direction.BUY)
        sell_rows = evaluate(events, close, Direction.SELL)
        sections.append(
            f"## {name}\n\n"
            f"- BUY 信号数: {len(buy_events)}，SELL 信号数: {len(sell_events)}\n\n"
            "### BUY 信号后续表现\n\n"
            "| 持有期 | 信号数 | 胜率(上涨) | 平均收益 |\n|---|---|---|---|\n"
            + "\n".join(buy_rows)
            + "\n\n### SELL 信号后续表现（胜率=信号后确实下跌）\n\n"
            "| 持有期 | 信号数 | 胜率(下跌) | 平均收益(反向) |\n|---|---|---|---|\n"
            + "\n".join(sell_rows)
            + "\n"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"new_strategies_from{args.start:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# RSI/MACD/布林带回测报告（日线事件驱动）\n\n"
        f"- 数据: {args.start} 至今，universe={settings.universe}\n"
        "- 逐日截取数据模拟真实盘前调用，import 的是 src 下同一份策略代码\n\n"
        + "\n".join(sections)
        + "\n> 回测结果仅供评估，不构成投资建议。参数修改由用户决定。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
