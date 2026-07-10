"""滚动样本外比较：基线 vs 排名缓冲 vs 绝对动量门槛。

这是研究门禁，不直接修改生产配置。每个窗口只用训练段挑选候选，再在紧随其后的
测试段评价；撮合统一为收盘生成信号、下一交易日开盘成交并计 5bp 手续费+5bp 滑点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.oos_validation import RollingSplit, buffered_selection, rolling_splits
from quant_signal.research_execution import build_next_open_portfolio

REPORTS = Path(__file__).parent / "reports"


@dataclass(frozen=True)
class Candidate:
    name: str
    rank_buffer: int
    min_momentum: float


CANDIDATES = (
    Candidate("baseline", 0, float("-inf")),
    Candidate("buffer_2", 2, float("-inf")),
    Candidate("absolute_0", 0, 0.0),
    Candidate("buffer_2_absolute_0", 2, 0.0),
)


def _weights(
    close: pd.DataFrame,
    split_start: pd.Timestamp,
    split_end: pd.Timestamp,
    candidate: Candidate,
    *,
    lookback: int = 60,
    top_n: int = 5,
) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    eval_close = close.loc[(close.index >= split_start) & (close.index <= split_end)]
    month_ends = eval_close.groupby(eval_close.index.tz_localize(None).to_period("M")).tail(1).index
    current: set[str] = set()
    for ts in month_ends:
        ranking: list[tuple[str, float]] = []
        for ticker in close.columns:
            series = close.loc[:ts, ticker].dropna()
            if len(series) >= lookback + 1:
                ranking.append((str(ticker), float(series.iloc[-1] / series.iloc[-lookback - 1] - 1)))
        ranking.sort(key=lambda item: item[1], reverse=True)
        selected = buffered_selection(
            ranking,
            current,
            top_n=top_n,
            rank_buffer=candidate.rank_buffer,
            min_momentum=candidate.min_momentum,
        )
        weights.loc[ts:, :] = 0.0
        if selected:
            weights.loc[ts:, selected] = 1.0 / len(selected)
        current = set(selected)
    return weights


def _metrics(
    bars: pd.DataFrame, split: RollingSplit, candidate: Candidate, *, test: bool
) -> dict[str, float]:
    start = split.test_start if test else split.train_start
    end = split.test_end if test else split.train_end
    close = bars["close"].unstack("ticker").sort_index()
    open_prices = bars["open"].unstack("ticker").reindex_like(close)
    weights = _weights(close, start, end, candidate)
    mask = (close.index >= start) & (close.index <= end)
    eval_weights = weights.loc[mask]
    pf = build_next_open_portfolio(close.loc[mask], open_prices.loc[mask], eval_weights)
    stats = pf.stats()
    years = max((end - start).days / 365.25, 1 / 365.25)
    return {
        "return": float(stats["Total Return [%]"]),
        "sharpe": float(stats["Sharpe Ratio"]),
        "maxdd": float(stats["Max Drawdown [%]"]),
        "turnover": float(eval_weights.diff().abs().sum(axis=1).sum() / 2 / years),
    }


def main() -> None:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    bars = store.read_daily_bars(settings.universe)
    dates = pd.DatetimeIndex(bars.index.get_level_values("ts").unique()).sort_values()
    splits = rolling_splits(dates, train_size=756, test_size=252, step_size=252)
    if not splits:
        raise RuntimeError("历史不足：滚动样本外至少需要 1008 个共同交易日")

    rows = [
        "| 训练段 | 测试段 | 训练胜出候选 | 测试收益% | vs基线pp | Sharpe | ΔSharpe | 年换手 | vs基线 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    selected_names: list[str] = []
    test_sharpes: list[float] = []
    improved_sharpe = 0
    reduced_turnover = 0
    return_deltas: list[float] = []
    for split in splits:
        trained = [(candidate, _metrics(bars, split, candidate, test=False)) for candidate in CANDIDATES]
        winner, _ = max(trained, key=lambda item: item[1]["sharpe"])
        result = _metrics(bars, split, winner, test=True)
        baseline = _metrics(bars, split, CANDIDATES[0], test=True)
        selected_names.append(winner.name)
        test_sharpes.append(result["sharpe"])
        return_delta = result["return"] - baseline["return"]
        sharpe_delta = result["sharpe"] - baseline["sharpe"]
        turnover_delta = result["turnover"] - baseline["turnover"]
        return_deltas.append(return_delta)
        improved_sharpe += sharpe_delta > 0
        reduced_turnover += turnover_delta < 0
        rows.append(
            f"| {split.train_start.date()}~{split.train_end.date()} | "
            f"{split.test_start.date()}~{split.test_end.date()} | {winner.name} | "
            f"{result['return']:+.1f} | {return_delta:+.1f} | {result['sharpe']:.2f} | "
            f"{sharpe_delta:+.2f} | {result['turnover']:.1f} | {turnover_delta:+.1f} |"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"oos_validation_run{datetime.now(timezone.utc):%Y%m%d}.md"
    counts = pd.Series(selected_names).value_counts().to_dict()
    majority = len(splits) // 2 + 1
    avg_return_delta = sum(return_deltas) / len(return_deltas)
    passed = (
        improved_sharpe >= majority
        and reduced_turnover >= majority
        and avg_return_delta > 5.0
    )
    verdict = "通过初筛，可进入生产同口径复验" if passed else "未通过，不修改生产参数"
    out.write_text(
        "# 排名缓冲/绝对动量滚动样本外报告\n\n"
        "- 训练 756 交易日、测试 252 交易日、每 252 日向前滚动。\n"
        "- 月末收盘选股、下一交易日开盘成交；单边手续费5bp+滑点5bp。\n"
        "- 当前自选池且非 point-in-time，仍有幸存者偏差；本报告只决定候选是否值得进一步验证。\n"
        "- 为隔离变量，本实验使用全局 Top5，不等同生产的币种/资产类型分组配额。\n\n"
        + "\n".join(rows)
        + f"\n\n候选胜出次数：{counts}；测试窗平均 Sharpe={sum(test_sharpes) / len(test_sharpes):.2f}。\n"
        + f"相对基线：Sharpe改善 {improved_sharpe}/{len(splits)} 窗，换手下降 "
        + f"{reduced_turnover}/{len(splits)} 窗，平均收益差 {avg_return_delta:+.1f}pp。\n"
        + f"\n门禁裁决：**{verdict}**。多数窗口需同时改善风险调整收益和换手，且平均收益差超过既有约 5pp 时点噪声带。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
