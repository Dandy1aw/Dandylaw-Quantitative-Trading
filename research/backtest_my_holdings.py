"""专门针对用户 9 只真实持仓的回测：MU, DRAM, RAM, MRVL, SNDK, SNXX, SMH, QQQM, SPY

复用 research/backtest_momentum.py 和 backtest_new_strategies.py 里已经验证过的
函数，只是把喂进去的 bars 限定到这 9 只标的——MomentumRotation/RsiReversion/
MacdCross/BollingerBreakout 的 generate() 都会自动按 bars 里实际存在的列筛选
universe，不需要改任何策略代码。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.strategies.base import Direction
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.rsi_reversion import RsiReversion

from backtest_momentum import run_backtest as momentum_run_backtest
from backtest_new_strategies import collect_events, evaluate

HOLDINGS = ["MU", "DRAM", "RAM", "MRVL", "SNDK", "SNXX", "SMH", "QQQM", "SPY"]
REPORTS = Path(__file__).parent / "reports"


def main() -> None:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    bars = store.read_daily_bars(HOLDINGS)
    print("拿到的数据行数:", len(bars), "覆盖标的:", sorted(bars.index.get_level_values("ticker").unique()))
    for t in HOLDINGS:
        n = store.daily_bar_count(t)
        print(f"  {t}: {n} 根日线")

    close = bars["close"].unstack("ticker").sort_index()

    mp = settings.strategies["momentum_rotation"]
    m = momentum_run_backtest(
        bars, int(mp["lookback_days"]), int(mp["top_n"]), float(mp["min_dollar_volume"]), {}
    )

    rp = settings.strategies["rsi_reversion"]
    mc = settings.strategies["macd_cross"]
    bb = settings.strategies["bollinger_breakout"]
    strategies = {
        "RSI回归": RsiReversion(HOLDINGS, int(rp["period"]), float(rp["oversold"]), float(rp["overbought"])),
        "MACD金叉死叉": MacdCross(HOLDINGS, int(mc["fast"]), int(mc["slow"]), int(mc["signal"])),
        "布林带突破": BollingerBreakout(HOLDINGS, int(bb["period"]), float(bb["num_std"])),
    }

    sections = [
        "## 动量轮动（组合层面）\n\n"
        f"| 年化收益 | Sharpe | 最大回撤 | 年换手次数 |\n|---|---|---|---|\n"
        f"| {m['annual_return']:.1f}% | {m['sharpe']:.2f} | {m['max_drawdown']:.1f}% | {m['turnover_per_year']:.1f} |\n"
    ]
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

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"my_holdings_{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 真实持仓专属回测报告\n\n"
        f"- 标的: {HOLDINGS}\n"
        "- 数据: research 缓存(bars_research.duckdb)已有历史，含2016至今\n"
        "- 动量轮动只在这9只标的内部排名(不再跟其余候选ETF/个股竞争)，"
        "结果跟universe=20只时的报告会不同\n\n"
        + "\n".join(sections)
        + "\n> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
