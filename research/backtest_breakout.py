"""20日突破策略回测（日线近似）。

历史 5min 数据 yfinance 只保留 60 天，无法回测多年，故用日线近似——
逐日截取 bars 喂 Breakout20d.generate（与实时同一份代码），统计信号
后 5/10/20 日表现。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.breakout_20d import Breakout20d

REPORTS = Path(__file__).parent / "reports"
HORIZONS = (5, 10, 20)


def load_bars(start: date) -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = settings.watchlist
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(
            tickers, start, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    return store.read_daily_bars(tickers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    settings = load_settings()
    bp = settings.strategies["breakout_20d"]
    strat = Breakout20d(
        universe=settings.watchlist,
        high_lookback_days=int(bp["high_lookback_days"]),
        volume_multiplier=float(bp["volume_multiplier"]),
    )
    bars = load_bars(args.start)
    close = bars["close"].unstack("ticker").sort_index()
    all_ts = close.index

    events: list[tuple[pd.Timestamp, str, float]] = []
    for i in range(21, len(all_ts)):
        window = bars[bars.index.get_level_values("ts") <= all_ts[i]]
        for s in strat.generate(window):
            events.append((all_ts[i], s.ticker, s.price))

    rows = ["| 持有期 | 有效信号数 | 胜率 | 平均收益 |", "|---|---|---|---|"]
    for h in HORIZONS:
        rets: list[float] = []
        for ts, ticker, price in events:
            pos = all_ts.get_loc(ts)
            if pos + h < len(all_ts):
                exit_px = float(close[ticker].iloc[pos + h])
                rets.append(exit_px / price - 1)
        if rets:
            sr = pd.Series(rets)
            rows.append(f"| {h}日 | {len(sr)} | {(sr > 0).mean():.0%} | {sr.mean():+.2%} |")
        else:
            rows.append(f"| {h}日 | 0 | - | - |")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"breakout_{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 20日突破回测报告（日线近似）\n\n"
        f"- 数据: {args.start} 至今，watchlist={settings.watchlist}\n"
        f"- 信号总数: {len(events)}\n"
        "- 注: 历史 5min 数据不可得，以日线收盘突破近似盘中信号，结果偏保守\n\n"
        + "\n".join(rows)
        + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}（信号 {len(events)} 个）")


if __name__ == "__main__":
    main()
