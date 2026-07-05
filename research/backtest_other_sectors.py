"""跨板块10只美股的探索性回测：JPM(金融)/XOM(能源)/CAT(工业)/PG(消费必需)/
UNH(医疗)/NEE(公用事业)/PLD(房地产)/DIS(传媒)/HD(消费可选)/LIN(材料)

目的：现有 universe 高度集中在半导体/存储芯片，用这10只分散在不同板块的
龙头股探索"加入更分散的板块"是否有助于降低回撤、提升风险调整后收益。
这是探索性分析，不会自动改动 settings.yaml 的正式 universe。

复用 backtest_momentum.py 的 run_backtest 和 backtest_new_strategies.py 的
collect_events/evaluate，同一份策略代码。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.base import Direction
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.rsi_reversion import RsiReversion

sys.path.insert(0, str(Path(__file__).parent))
from backtest_momentum import run_backtest as momentum_run_backtest
from backtest_new_strategies import collect_events, evaluate

TICKERS = ["JPM", "XOM", "CAT", "PG", "UNH", "NEE", "PLD", "DIS", "HD", "LIN"]
SECTORS = {
    "JPM": "金融", "XOM": "能源", "CAT": "工业", "PG": "消费必需", "UNH": "医疗",
    "NEE": "公用事业", "PLD": "房地产", "DIS": "传媒", "HD": "消费可选", "LIN": "材料",
}
REPORTS = Path(__file__).parent / "reports"
START = date(2016, 1, 1)


def load_bars() -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    have = min((store.daily_bar_count(t) for t in TICKERS), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(TICKERS, START, datetime.now(timezone.utc).date())
        store.write_daily_bars(df, source="yfinance")
    start_dt = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
    return store.read_daily_bars(TICKERS, start=start_dt)


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    print("拿到数据的标的:", sorted(bars.index.get_level_values("ticker").unique()))
    for t in TICKERS:
        store = BarStore(settings.db_path / "bars_research.duckdb")
        print(f"  {t} ({SECTORS[t]}): {store.daily_bar_count(t)} 根日线")

    close = bars["close"].unstack("ticker").sort_index()

    mp = settings.strategies["momentum_rotation"]
    m = momentum_run_backtest(
        bars, int(mp["lookback_days"]), int(mp["top_n"]), float(mp["min_dollar_volume"]), {},
        universe=TICKERS,
    )

    rp = settings.strategies["rsi_reversion"]
    mc = settings.strategies["macd_cross"]
    bb = settings.strategies["bollinger_breakout"]
    strategies = {
        "RSI回归": RsiReversion(TICKERS, int(rp["period"]), float(rp["oversold"]), float(rp["overbought"])),
        "MACD金叉死叉": MacdCross(TICKERS, int(mc["fast"]), int(mc["slow"]), int(mc["signal"])),
        "布林带突破": BollingerBreakout(TICKERS, int(bb["period"]), float(bb["num_std"])),
    }

    sections = [
        "## 动量轮动（组合层面，10只跨板块股票内部排名）\n\n"
        "| 年化收益 | Sharpe | 最大回撤 | 年换手次数 |\n|---|---|---|---|\n"
        f"| {m['annual_return']:.1f}% | {m['sharpe']:.2f} | {m['max_drawdown']:.1f}% | {m['turnover_per_year']:.1f} |\n"
    ]
    per_ticker_rows = []
    for name, strat in strategies.items():
        events = collect_events(strat, bars)
        buy_events = [e for e in events if e[3] == Direction.BUY]
        sell_events = [e for e in events if e[3] == Direction.SELL]
        buy_rows = evaluate(events, close, Direction.BUY)
        sell_rows = evaluate(events, close, Direction.SELL)
        sections.append(
            f"## {name}\n\n"
            f"- BUY 信号数: {len(buy_events)}，SELL 信号数: {len(sell_events)}\n\n"
            "### BUY 信号后续表现\n\n| 持有期 | 信号数 | 胜率(上涨) | 平均收益 |\n|---|---|---|---|\n"
            + "\n".join(buy_rows)
            + "\n\n### SELL 信号后续表现（胜率=信号后确实下跌）\n\n"
            "| 持有期 | 信号数 | 胜率(下跌) | 平均收益(反向) |\n|---|---|---|---|\n"
            + "\n".join(sell_rows) + "\n"
        )

        # 按标的拆开算20日BUY表现，用于板块对比
        all_ts = close.index
        by_ticker: dict[str, list[float]] = {}
        for ts, ticker, price, d in events:
            if d != Direction.BUY or ticker not in close.columns:
                continue
            pos = all_ts.get_loc(ts)
            if pos + 20 < len(all_ts):
                exit_px = float(close[ticker].iloc[pos + 20])
                by_ticker.setdefault(ticker, []).append(exit_px / price - 1.0)
        for ticker, rets in by_ticker.items():
            if len(rets) < 20:   # 门槛比之前低一些，因为只有10只票总信号数天然更少
                continue
            sr = pd.Series(rets)
            per_ticker_rows.append(
                {
                    "ticker": ticker, "sector": SECTORS[ticker], "strategy": name,
                    "signals": len(sr), "win_rate": float((sr > 0).mean()),
                    "avg_return": float(sr.mean()),
                }
            )

    per_ticker_section = ""
    if per_ticker_rows:
        by_win = sorted(per_ticker_rows, key=lambda r: r["win_rate"], reverse=True)[:3]  # type: ignore[arg-type,return-value]
        by_ret = sorted(per_ticker_rows, key=lambda r: r["avg_return"], reverse=True)[:3]  # type: ignore[arg-type,return-value]

        def fmt(rows: list[dict[str, object]]) -> str:
            lines = ["| 标的 | 板块 | 策略 | 信号数 | 胜率 | 平均收益(20日) |", "|---|---|---|---|---|---|"]
            for r in rows:
                lines.append(
                    f"| {r['ticker']} | {r['sector']} | {r['strategy']} | {r['signals']} |"
                    f" {r['win_rate']:.0%} | {r['avg_return']:+.2%} |"
                )
            return "\n".join(lines)

        per_ticker_section = (
            "\n## 按标的拆分（信号数≥20才参与排名）\n\n"
            "### 胜率最高 Top 3\n\n" + fmt(by_win)
            + "\n\n### 平均收益最高 Top 3\n\n" + fmt(by_ret) + "\n"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"other_sectors_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 跨板块10只美股探索性回测\n\n"
        f"- 标的: {[(t, SECTORS[t]) for t in TICKERS]}\n"
        f"- 数据: {START} 至今\n"
        "- 探索性分析：现有 universe 高度集中半导体/存储芯片，这里测试分散到"
        "不同板块龙头股是否有意义，不会自动改动正式 universe\n\n"
        + "\n".join(sections)
        + per_ticker_section
        + "\n> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
