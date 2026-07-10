from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Sequence
from zoneinfo import ZoneInfo

from quant_signal.notifier.base import Card, CardKind
from quant_signal.strategies.base import Direction, Signal

if TYPE_CHECKING:
    from quant_signal.account import AccountState
    from quant_signal.execution import ExecutionPlan

_SGT = ZoneInfo("Asia/Singapore")
_ET = ZoneInfo("America/New_York")
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


_PAPER_FOOTER = "> PAPER 模拟账户建议，仅供观察，不构成投资建议；本系统不自动下单。"


def _fmt_qty(value: object) -> str:
    return str(value) if value is not None else "-"


def _fmt_price(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def execution_plan_card(
    account: "AccountState | None",
    plans: "Sequence[ExecutionPlan]",
    now: datetime,
) -> Card:
    lines: list[str] = []
    if account is not None:
        snap = account.snapshot
        account_time = snap.retrieved_at.astimezone(_SGT).strftime("%Y-%m-%d %H:%M")
        lines += [
            f"**账户时间**: {account_time} (SGT)",
            f"**权益** {snap.equity} {snap.currency} · **现金** {snap.cash} ·"
            f" **购买力** {snap.buying_power}",
        ]
        if account.positions:
            lines += ["", "**持仓**", "| 标的 | 数量 | 成本 | 市值 |", "|---|---|---|---|"]
            lines += [
                f"| {p.symbol} | {p.qty} | {p.avg_entry_price} | {p.market_value} |"
                for p in account.positions
            ]
        if account.open_orders:
            lines += ["", "**未成交订单**"]
            lines += [
                f"- {o.symbol} {o.side.upper()} {o.qty} @ {o.limit_price or '市价'}"
                f" ({o.status})"
                for o in account.open_orders
            ]
        if account.recent_orders:
            lines += ["", "**最近成交**"]
            lines += [
                f"- {o.order_id}: {o.symbol} {o.side.upper()} {o.filled_qty}"
                f" @ {o.filled_avg_price or '-'}"
                for o in account.recent_orders[:5]
            ]
    else:
        lines.append("⚠ **账户数据不足，未计算股数**（仅展示观察价位）")

    if plans:
        lines += [
            "",
            "**执行计划**",
            "| 标的 | 状态 | 买入区 | 限价 | 建议股数 | 金额 | 止损 | 止盈 | 有效期 | 备注 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for plan in plans:
            expiry = plan.expires_at.astimezone(_ET).strftime("%m-%d %H:%M ET")
            note = plan.block_reason or "+".join(plan.source_strategies)
            lines.append(
                f"| {plan.ticker} ({plan.currency}) | {plan.state.value} |"
                f" {plan.entry_low:.2f}-{plan.entry_high:.2f} |"
                f" {plan.limit_price:.2f} | {_fmt_qty(plan.suggested_qty)} |"
                f" {_fmt_price(plan.suggested_notional)} | {plan.stop_loss:.2f} |"
                f" {plan.take_profit:.2f} | {expiry} | {note} |"
            )
    else:
        lines += ["", "今日无执行候选。"]
    lines += ["", _PAPER_FOOTER]
    return Card(
        kind=CardKind.REPORT,
        title="🧭 PAPER 执行计划 · 盘前",
        body_md="\n".join(lines),
    )


def plan_event_card(
    plan: "ExecutionPlan", event: str, *, price: float | None, at: datetime
) -> Card:
    sgt = at.astimezone(_SGT).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"**现价**: {_fmt_price(price)}",
        f"**买入区**: {plan.entry_low:.2f}-{plan.entry_high:.2f} ·"
        f" **限价** {plan.limit_price:.2f}",
        f"**建议股数**: {_fmt_qty(plan.suggested_qty)} ·"
        f" **金额** {_fmt_price(plan.suggested_notional)}",
        f"**止损**: {plan.stop_loss:.2f} · **止盈**: {plan.take_profit:.2f}",
        f"**时间**: {sgt} (SGT)",
        "",
        _PAPER_FOOTER,
    ]
    return Card(
        kind=CardKind.SIGNAL,
        title=f"⚡ PAPER 执行提醒 · {plan.ticker} {event}",
        body_md="\n".join(lines),
        url=f"https://www.tradingview.com/chart/?symbol={plan.ticker}",
    )


def build_ai_briefing_card(body_md: str) -> Card:
    disclaimer = "仅供观察，不构成投资建议"
    body = body_md.strip()
    if disclaimer not in body:
        body = f"{body}\n\n_{disclaimer}_"
    return report_card("🤖 AI早报观点", body)


def negative_overreaction_card(cases: list[dict[str, object]]) -> Card:
    lines = [
        "仅观察：利空分类未发现结构性损伤，且价格已出现企稳；不代表已证明错杀。",
        "",
        "| 标的 | 事件类型 | 冲击跌幅 | 放量 | 确认日 | 下一步 |",
        "|---|---|---|---|---|---|",
    ]
    for case in cases:
        drop = case["drop"]
        volume_ratio = case["volume_ratio"]
        assert isinstance(drop, (int, float))
        assert isinstance(volume_ratio, (int, float))
        lines.append(
            f"| {case['ticker']} | {case['event_type']} | {float(drop):+.1%} | "
            f"{float(volume_ratio):.1f}x | {case['confirmed_at']} | 虚拟观察 |"
        )
    return report_card("🩹 利空错杀 · 企稳观察", "\n".join(lines))


def momentum_ranking_card(
    ranking: list[tuple[str, float, float]],
    held: set[str],
    trend_flat: set[str] | None = None,
    insufficient: set[str] | None = None,
    footer_md: str | None = None,
) -> Card:
    """Render a display-only whole-universe momentum ranking.

    Selection remains controlled by the market/asset-group quotas.  This card
    deliberately exposes the untruncated ranking so weak held positions and
    strong candidates outside those quotas stay visible.
    """
    flat = trend_flat or set()
    short_history = insufficient or set()

    def label(ticker: str) -> str:
        marks: list[str] = []
        if ticker in held:
            marks.append("⚠持仓")
        if ticker in flat:
            marks.append("⚠趋势FLAT")
        if ticker in short_history:
            marks.append("⚠数据不足")
        return " ".join([ticker, *marks])

    def rows(items: list[tuple[str, float, float]]) -> list[str]:
        return [
            f"| {label(ticker)} | {momentum:+.1%} | {price:.2f} |"
            for ticker, momentum, price in items
        ]

    top = ranking[:5]
    warning_by_ticker = {ticker: (ticker, momentum, price) for ticker, momentum, price in ranking[-3:]}
    for item in ranking:
        if item[0] in flat:
            warning_by_ticker.setdefault(item[0], item)
    warning = sorted(warning_by_ticker.values(), key=lambda item: item[1])

    parts = [
        "**Top 5 买入候选**",
        "| 标的 | 60日动量 | 参考价 |",
        "|---|---|---|",
        *rows(top),
        "",
        "**Bottom 3 卖出警示**",
        "| 标的 | 60日动量 | 参考价 |",
        "|---|---|---|",
        *rows(warning),
    ]
    if footer_md:
        parts += ["", footer_md]
    return report_card("📊 动量全池榜单", "\n".join(parts))


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
            "| 标的 | 方向 | 参考价 | 现价 | 目标买入价 | 止盈目标 | 止损价 | 原因 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in ssigs:
            live = live_prices.get(s.ticker)
            live_str = f"{live:.2f}" if live is not None else "-"
            extra = s.extra or {}
            tp = extra.get("take_profit")
            tp_str = f"{float(tp):.2f}" if isinstance(tp, (int, float)) else "-"
            sl = extra.get("stop_loss")
            if isinstance(sl, (int, float)):
                # 止损已在现价上方 = 已破位（应离场），标注避免"止损>现价"的困惑
                sl_str = f"{float(sl):.2f}" + (" ⚠破位" if float(sl) >= s.price else "")
            else:
                sl_str = "-"
            target = extra.get("target_buy")
            if isinstance(target, (int, float)):
                band_str = f"{float(target):.2f}"
                if extra.get("overheat"):
                    band_str += " ⚠过热"
            else:
                band_str = "-"
            reason = s.reason
            hold = extra.get("holding_return")
            if isinstance(hold, (int, float)):
                reason = f"{reason}，持有期 {float(hold):+.1%}"
            earnings_days = extra.get("earnings_in_days")
            if isinstance(earnings_days, int):
                label = "今日财报" if earnings_days == 0 else f"{earnings_days}天后财报"
                reason = f"{reason}，⚠{label}"
            quality = extra.get("quality_flag")
            if isinstance(quality, str) and quality:
                reason = f"{reason}，⚠质量差({quality})"
            surprise = extra.get("earnings_surprise")
            if isinstance(surprise, (int, float)):
                if surprise >= 0:
                    reason = f"{reason}，财报超预期+{float(surprise):.0f}%"
                else:
                    reason = f"{reason}，⚠财报不及预期{float(surprise):.0f}%"
            lines.append(
                f"| {s.ticker} | {s.direction.value.upper()} | {s.price:.2f} |"
                f" {live_str} | {band_str} | {tp_str} | {sl_str} | {reason} |"
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
