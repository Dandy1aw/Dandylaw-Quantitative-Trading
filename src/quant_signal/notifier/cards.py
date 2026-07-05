from __future__ import annotations

from zoneinfo import ZoneInfo

from quant_signal.notifier.base import Card, CardKind
from quant_signal.strategies.base import Direction, Signal

_SGT = ZoneInfo("Asia/Singapore")
_DIRECTION_EMOJI = {"buy": "📈", "sell": "📉", "reduce": "⚖️"}


def signal_card(s: Signal, delayed: bool = False) -> Card:
    emoji = _DIRECTION_EMOJI.get(s.direction.value, "📌")
    sgt = s.ts.astimezone(_SGT).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"**现价**: {s.price}",
        f"**触发原因**: {s.reason}",
    ]
    if s.suggested_weight is not None:
        lines.append(f"**建议仓位**: {s.suggested_weight:.0%}")
    lines += [f"**策略**: {s.strategy_id}", f"**时间**: {sgt} (SGT)"]
    if delayed:
        lines.append("⚠️ 数据延迟约15分钟，仅供观察")
    return Card(
        kind=CardKind.SIGNAL,
        title=f"{emoji} {s.ticker} {s.direction.value.upper()}",
        body_md="\n".join(lines),
        url=f"https://www.tradingview.com/chart/?symbol={s.ticker}",
    )


def report_card(title: str, body_md: str) -> Card:
    return Card(kind=CardKind.REPORT, title=title, body_md=body_md)


def alert_card(title: str, body_md: str) -> Card:
    return Card(kind=CardKind.ALERT, title=f"🚨 {title}", body_md=body_md)


_BEARISH_KEYWORDS = ("看空", "谨慎")


def build_enrichment_card(results: list[dict[str, object]], held: set[str]) -> Card:
    """UZI-Skill 深度分析摘要卡：持仓/今日BUY目标里，若深度分析给出看空/谨慎
    结论，跟我们量化信号方向相反，额外标注分歧提示。"""
    lines = ["| 标的 | 综合评分 | 结论 | 主要风险 |", "|---|---|---|---|"]
    for r in results:
        ticker = str(r["ticker"])
        score = float(r["overall_score"])  # type: ignore[arg-type]
        verdict = str(r["verdict_label"])
        risk_list = r["risks"]
        assert isinstance(risk_list, list)
        risks = ", ".join(str(x) for x in risk_list[:2])
        # 只看"·"前的结论headline（如"谨慎"），忽略后面"N派看多/M派看空"的
        # 分歧计数部分——那部分即使是看多结论也常含"看空"字样（少数派计数）
        headline = verdict.split("·")[0].strip()
        is_divergent = ticker in held and (
            score < 50 or any(k in headline for k in _BEARISH_KEYWORDS)
        )
        prefix = "⚠️ " if is_divergent else ""
        suffix = "（与量化信号方向分歧）" if is_divergent else ""
        lines.append(f"| {prefix}{ticker} | {score:.1f} | {verdict}{suffix} | {risks} |")
    return Card(kind=CardKind.REPORT, title="🔍 深度分析", body_md="\n".join(lines))


_STRATEGY_LABELS_ORDERED: list[tuple[str, str]] = [
    ("momentum_rotation", "动量轮动"),
    ("macd_cross", "MACD"),
    ("rsi_reversion", "RSI回归"),
    ("bollinger_breakout", "布林带"),
]
_STRATEGY_LABEL = dict(_STRATEGY_LABELS_ORDERED)
_STRATEGY_ORDER = [sid for sid, _ in _STRATEGY_LABELS_ORDERED]

_MARKET_BY_CCY = {"HKD": "港股组", "KRW": "韩股组"}
_MARKET_ORDER = ["美股组", "港股组", "韩股组"]   # 美股优先


def _market_of(ticker: str, international_tickers: dict[str, str]) -> str:
    return _MARKET_BY_CCY.get(international_tickers.get(ticker, ""), "美股组")


def _sig_sort_key(s: Signal) -> tuple[int, float, str]:
    # 小节内排序：BUY 在前，再按动量降序（动量信号带 momentum_60d）；无动量的
    # 技术信号按 ticker。用组内名次排序会让美股个股/ETF 交错，故改用动量。
    dir_rank = 0 if s.direction == Direction.BUY else 1
    mom = (s.extra or {}).get("momentum_60d")
    mom_key = -float(mom) if isinstance(mom, (int, float)) else 0.0
    return (dir_rank, mom_key, s.ticker)


def _confluence_rows(
    signals: list[Signal], international_tickers: dict[str, str]
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Signal]] = {}
    for s in signals:
        groups.setdefault((s.ticker, s.direction.value), []).append(s)
    rows: list[dict[str, object]] = []
    for (ticker, direction), sigs in groups.items():
        strat_ids = {s.strategy_id for s in sigs}
        if len(strat_ids) < 2:
            continue
        hit = [_STRATEGY_LABEL[sid] for sid in _STRATEGY_ORDER if sid in strat_ids]
        rows.append(
            {"ticker": ticker, "direction": direction, "hit": "+".join(hit),
             "price": sigs[0].price}
        )
    rows.sort(key=lambda r: (_MARKET_ORDER.index(
        _market_of(str(r["ticker"]), international_tickers)), str(r["ticker"])))
    return rows


def _confluence_card(rows: list[dict[str, object]]) -> Card:
    lines = ["| 标的 | 方向 | 命中策略 | 参考价 |", "|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {str(r['direction']).upper()} | {r['hit']} |"
            f" {float(r['price']):.2f} |"  # type: ignore[arg-type]
        )
    return Card(kind=CardKind.REPORT, title="🔥 【重要】多策略共振", body_md="\n".join(lines))


def _market_card(
    market: str, signals: list[Signal], live_prices: dict[str, float | None]
) -> Card:
    parts: list[str] = []
    for sid in _STRATEGY_ORDER:
        ssigs = sorted((s for s in signals if s.strategy_id == sid), key=_sig_sort_key)
        if not ssigs:
            continue
        lines = [
            f"**【{_STRATEGY_LABEL[sid]}】**",
            "| 标的 | 方向 | 参考价 | 现价 | 参考卖出价 | 原因 |",
            "|---|---|---|---|---|---|",
        ]
        for s in ssigs:
            live = live_prices.get(s.ticker)
            live_str = f"{live:.2f}" if live is not None else "-"
            sr = (s.extra or {}).get("sell_ref")
            sell_str = f"{float(sr):.2f}" if isinstance(sr, (int, float)) else "-"
            lines.append(
                f"| {s.ticker} | {s.direction.value.upper()} | {s.price:.2f} |"
                f" {live_str} | {sell_str} | {s.reason} |"
            )
        parts.append("\n".join(lines))
    return report_card(f"📋 盘前早报 · {market}", "\n\n".join(parts))


def premarket_cards(
    signals: list[Signal],
    international_tickers: dict[str, str],
    live_prices: dict[str, float | None],
) -> list[Card]:
    """把当天推送的盘前信号渲染成有序卡片：
    [可选【重要】共振卡] + 美股组 + 港股组 + 韩股组（跳过空卡，美股优先）。"""
    cards: list[Card] = []
    confl = _confluence_rows(signals, international_tickers)
    if confl:
        cards.append(_confluence_card(confl))
    for market in _MARKET_ORDER:
        msigs = [s for s in signals if _market_of(s.ticker, international_tickers) == market]
        if msigs:
            cards.append(_market_card(market, msigs, live_prices))
    return cards
