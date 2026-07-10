"""利空错杀事件回测：价格对照 / 硬否决 / 严格可修复消息三组。

先从本地日线筛出价格冲击并确认企稳，只对这些窄事件窗请求 Alpaca 历史新闻。
这既减少 API 调用，也确保三组使用完全相同的价格候选和成交规则。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_signal.config import load_settings
from quant_signal.datafeed.news import AlpacaNewsSource, NewsArticle
from quant_signal.datafeed.store import BarStore
from quant_signal.news_classifier import classify_news
from quant_signal.news_event_loader import NewsWindow, fetch_news_windows
from quant_signal.news_store import NewsStore, StoredNews
from quant_signal.overreaction_backtest import EventTrade, simulate_trade, summarize_trades
from quant_signal.strategies.negative_overreaction import confirm_reversal, detect_shock

REPORTS = Path(__file__).parent / "reports"
ROUND_TRIP_SIDE_COST = 0.0015  # 每边 5bp fee + 10bp slippage


def _article(row: StoredNews) -> NewsArticle:
    return NewsArticle(
        row.article_id, row.created_at, row.updated_at, row.symbols,
        row.headline, row.summary, row.content, row.url, row.source,
    )


def _net(trade: EventTrade) -> EventTrade:
    return replace(
        trade,
        entry_price=trade.entry_price * (1 + ROUND_TRIP_SIDE_COST),
        exit_price=trade.exit_price * (1 - ROUND_TRIP_SIDE_COST),
    )


def _row(name: str, trades: list[EventTrade]) -> str:
    s = summarize_trades([_net(trade) for trade in trades])
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (
        f"| {name} | {s['trades']:.0f} | {s['win_rate']:.0%} | "
        f"{s['avg_return']:+.2%} | {s['median_return']:+.2%} | {pf} | {s['avg_mae']:+.2%} |"
    )


def main() -> None:
    settings = load_settings()
    price_store = BarStore(settings.db_path / "bars_research.duckdb")
    news_store = NewsStore(settings.db_path / "news_research.db")
    news_source = (
        AlpacaNewsSource(settings.alpaca_key, settings.alpaca_secret)
        if settings.alpaca_key and settings.alpaca_secret
        else None
    )
    tickers = [
        ticker for ticker in settings.universe
        if settings.asset_type.get(ticker) == "STOCK"
        and ticker not in settings.international_tickers
    ]
    bars = price_store.read_daily_bars(tickers)
    available = set(bars.index.get_level_values("ticker")) if not bars.empty else set()

    price_trades: list[EventTrade] = []
    hard_rule_trades: list[EventTrade] = []
    strict_news_trades: list[EventTrade] = []
    strict_details: list[tuple[EventTrade, str]] = []
    shocks = 0
    confirmed = 0
    news_failures: list[str] = []
    classified_events: dict[str, int] = {}
    events = []

    for ticker in tickers:
        if ticker not in available:
            continue
        sub = bars.xs(ticker, level="ticker").sort_index()
        for ts in sub.index[200:-42]:
            shock = detect_shock(ticker, sub, ts)
            if shock is None:
                continue
            shocks += 1
            confirmation = confirm_reversal(sub, shock)
            if confirmation is None:
                continue
            confirmed += 1
            trade = simulate_trade(sub, shock, confirmation)
            price_trades.append(trade)
            shock_dt = shock.shock_at.to_pydatetime().astimezone(timezone.utc)
            window = NewsWindow(ticker, shock_dt - timedelta(days=1), shock_dt + timedelta(days=1))
            events.append((ticker, shock, trade, window))

    missing_windows = []
    seen_keys = set()
    for ticker, _, _, window in events:
        if window.key in seen_keys:
            continue
        seen_keys.add(window.key)
        if not news_store.between([ticker], window.start, window.end):
            missing_windows.append(window)
    if news_source is not None and missing_windows:
        fetched_by_window, failures = fetch_news_windows(
            news_source, missing_windows, max_workers=6
        )
        for rows_for_window in fetched_by_window.values():
            news_store.put_many(rows_for_window, seen_at=datetime.now(timezone.utc))
        news_failures.extend(
            f"{key[0]}@{key[1].date()}: {message}"
            for key, message in failures.items()
        )

    for ticker, _, trade, window in events:
        cached = news_store.between([ticker], window.start, window.end)
        assessments = [classify_news(_article(row)) for row in cached]
        for assessment in assessments:
            classified_events[assessment.event_type] = (
                classified_events.get(assessment.event_type, 0) + 1
            )
        structural = any(item.structural_damage is True for item in assessments)
        if assessments and not structural:
            hard_rule_trades.append(trade)
        if not structural and any(not item.veto for item in assessments):
            strict_news_trades.append(trade)
            event_types = ",".join(sorted({item.event_type for item in assessments if not item.veto}))
            strict_details.append((trade, event_types))

    rows = [
        "| 方案 | 完成交易 | 胜率 | 平均净收益 | 中位净收益 | 盈利因子 | 平均MAE |",
        "|---|---|---|---|---|---|---|",
        _row("纯价格冲击+企稳", price_trades),
        _row("有新闻且无硬否决", hard_rule_trades),
        _row("严格可修复消息", strict_news_trades),
    ]
    strict = summarize_trades([_net(trade) for trade in strict_news_trades])
    gate = (
        len(strict_news_trades) >= 100
        and strict["avg_return"] > 0
        and strict["profit_factor"] > 1
    )
    verdict = "可进入观察盘二期" if gate else "样本或质量门禁未通过，仅保留观察卡"
    failure_note = "\n".join(f"- {line}" for line in news_failures[:10]) or "- 无"
    REPORTS.mkdir(exist_ok=True)
    detail_rows = [
        "| 标的 | 冲击日 | 消息类型 | 净收益 | 退出 | MAE |",
        "|---|---|---|---|---|---|",
    ]
    for trade, event_types in strict_details:
        net = _net(trade)
        detail_rows.append(
            f"| {trade.ticker} | {trade.shock_at.date()} | {event_types} | "
            f"{net.return_pct:+.2%} | {trade.exit_reason} | {trade.max_adverse_excursion:+.2%} |"
        )
    run_day = datetime.now(ZoneInfo("Asia/Shanghai"))
    out = REPORTS / f"negative_overreaction_run{run_day:%Y%m%d}.md"
    out.write_text(
        "# 利空错杀历史事件回测\n\n"
        f"- 标的：当前美股个股池 {len(tickers)} 只；非 point-in-time，存在幸存者偏差。\n"
        "- 价格：事件前在200日线上、单日跌幅≥max(8%,2ATR)、成交量≥1.8倍；至少等待2日企稳。\n"
        "- 成交：确认后下一交易日开盘；每边5bp手续费+10bp滑点；修复/止损均收盘确认后次日开盘。\n"
        f"- 检出冲击 {shocks} 个，完成企稳确认 {confirmed} 个。\n\n"
        + "\n".join(rows)
        + f"\n\n消息事件类型计数：{classified_events}\n"
        + "\n## 严格消息组逐笔明细\n\n"
        + "\n".join(detail_rows)
        + "\n"
        + f"\n## 门禁裁决\n\n**{verdict}**。正式门禁要求至少100笔样本，并改善价格对照的盈利因子和不利波动。\n"
        + "\n## 新闻请求失败（最多10条）\n\n"
        + failure_note
        + "\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
