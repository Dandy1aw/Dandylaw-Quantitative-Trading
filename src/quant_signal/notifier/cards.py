from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from quant_signal.notifier.base import Card, CardKind, CardSection
from quant_signal.notifier.briefing_format import (
    BLOCK_REASON_NAMES_ZH as _BLOCK_REASON_NAMES_ZH,
    briefing_float as _briefing_float,
    briefing_number as _briefing_number,
    briefing_percent as _briefing_percent,
    industry_name_zh as _industry_name_zh,
    market_cap_zh as _market_cap_zh,
    sector_name_zh as _sector_name_zh,
    usd_yi as _usd_yi,
)
from quant_signal.strategies.base import Direction, Signal

if TYPE_CHECKING:
    from quant_signal.account import AccountState
    from quant_signal.execution import ExecutionPlan
    from quant_signal.options_flow import (
        HoldingOptionFlowSnapshot,
        OptionContractVolume,
        OptionFlowChange,
        OptionFlowSnapshot,
    )
    from quant_signal.options_intel import OptionIntel

_SGT = ZoneInfo("Asia/Singapore")
_ET = ZoneInfo("America/New_York")
_DIRECTION_EMOJI = {"buy": "📈", "sell": "📉", "reduce": "⚖️"}
_SELL_RELIABILITY_NOTE = "⚠️ SELL 信号历史胜率偏低（回测 32–42%，牛市窗口），仅供参考"


def option_flow_card(
    snapshot: "OptionFlowSnapshot",
    changes: "Sequence[OptionFlowChange]",
    phase: str,
    *,
    previous: "OptionFlowSnapshot | None" = None,
    enrichment_status: Literal["ok", "failed", "off"] = "ok",
    display_dedupe: bool = True,
    display_sort_by_expiry: bool = True,
    held_underlyings: frozenset[str] = frozenset(),
    etf_underlyings: frozenset[str] = frozenset(),
    holding_snapshot: "HoldingOptionFlowSnapshot | None" = None,
) -> Card:
    """Compact underlying-level view of Cboe-visible Call/Put activity."""
    from quant_signal.options_flow import aggregate_underlying_flows

    # Retained for callers/config compatibility; the merged leaderboard no
    # longer performs one-contract-per-side display selection.
    del display_dedupe, display_sort_by_expiry

    phase_names = {
        "baseline": "首次榜",
        "change": "盘中异动",
        "holding_change": "持仓异动",
        "close": "收盘榜",
        "query": "即时查询",
    }
    phase_name = phase_names.get(phase, phase)
    observed = snapshot.captured_at.astimezone(_ET).strftime("%m/%d %H:%M ET")
    enrichment_label = {
        "ok": "Alpaca INDICATIVE · 约15分钟延迟",
        "failed": "Alpaca补全失败 · 本卡仅含Cboe成交量",
        "off": "未配置Alpaca补全 · 本卡仅含Cboe成交量",
    }[enrichment_status]
    held_legend = "\n📌 = 你的持仓标的" if held_underlyings else ""
    identity = CardSection(
        "**数据身份**\n"
        f"{phase_name}｜{observed}｜覆盖 {snapshot.venue_coverage:.0%}\n"
        "Cboe C1/C2/BZX/EDGX 四市场｜可见榜单量下限近似\n"
        f"补全：{enrichment_label}{held_legend}"
    )

    flag_names = {
        "NEW_TOP10": "新进Top10",
        "RANK_JUMP": "排名跃升",
        "VOLUME_SURGE": "成交加速",
        "HIGH_TURNOVER": "高换手",
    }

    def strike_text(value: Decimal) -> str:
        # Decimal 的 :g 不去尾零，normalize() 又会把 210.000 变成 2.1E+2
        return format(value.normalize(), "f")

    def money(value: Decimal) -> str:
        amount = float(value)
        if amount >= 1_000_000:
            return f"${amount / 1_000_000:.2f}M"
        if amount >= 1_000:
            return f"${amount / 1_000:.0f}K"
        return f"${amount:.0f}"

    focus_lines = ["**异动聚焦**"]
    if changes:
        for change in changes[:5]:
            item = change.contract
            prior_rank = f"#{change.previous_rank}" if change.previous_rank is not None else "新"
            labels = "、".join(flag_names.get(flag, flag) for flag in change.flags)
            context: list[str] = []
            if item.enrichment is not None:
                ratio = item.enrichment.volume_oi_ratio(item.volume)
                premium = item.enrichment.estimated_premium(item.volume)
                if ratio is not None:
                    context.append(f"V/OI {float(ratio):.1f}x")
                if premium is not None:
                    context.append(f"估算权利金 {money(premium)}")
            suffix = f"｜{'｜'.join(context)}" if context else ""
            side_label = "Call" if item.side == "call" else "Put"
            delta_label = (
                f"+{change.volume_delta:,}/15m"
                if change.volume_delta is not None
                else "首次可见"
            )
            held_mark = "📌 " if item.underlying in held_underlyings else ""
            focus_lines.append(
                f"{held_mark}{item.underlying} {item.expiration:%m/%d} "
                f"{strike_text(item.strike)} {side_label}｜"
                f"{labels}｜{prior_rank}→#{item.rank}｜"
                f"{delta_label}｜分数 {change.score}{suffix}"
            )
    else:
        focus_lines.append("暂无可比较的实质变化；保留原始Top10作为市场温度。")
    focus = CardSection("\n".join(focus_lines))

    holding_section: CardSection | None = None
    if holding_snapshot is not None:
        holding_lines = ["**📌 我的持仓期权**"]
        if not holding_snapshot.rows:
            holding_lines.append("当前没有可监控的真实持仓。")
        for holding_flow in holding_snapshot.rows:
            if holding_flow.data_status in ("no_chain", "unavailable"):
                holding_lines.append(
                    f"{holding_flow.underlying} · {holding_flow.structure_label}"
                )
                continue
            holding_lines.append(holding_flow.underlying)
            holding_lines.append(
                f"Call {holding_flow.call_volume:,} / Put {holding_flow.put_volume:,} "
                f"· 总量 {holding_flow.total_volume:,}"
            )
            holding_ratio = holding_flow.call_put_ratio
            ratio_text = "∞" if holding_ratio == float("inf") else (
                f"{holding_ratio:.2f}" if holding_ratio is not None else "-"
            )
            dominance = (
                f"{holding_flow.dominance:.0%}"
                if holding_flow.dominance is not None
                else "-"
            )
            direction = (
                "Call占优" if holding_flow.call_volume > holding_flow.put_volume
                else "Put占优" if holding_flow.put_volume > holding_flow.call_volume
                else "Call/Put均衡"
            )
            holding_lines.append(
                f"Call/Put {ratio_text} · {direction} {dominance}"
            )
            if holding_flow.data_status == "reset":
                holding_lines.append("15分钟增量不可比 · 累计量已重置")
            elif holding_flow.call_delta is None or holding_flow.put_delta is None:
                holding_lines.append("首次可见，无15分钟可比增量")
            else:
                holding_lines.append(
                    f"15分钟增量 Call +{holding_flow.call_delta:,} / "
                    f"Put +{holding_flow.put_delta:,}"
                )
        holding_section = CardSection("\n".join(holding_lines))

    def delta_text(value: int | None, partial: bool) -> str:
        if value is None:
            return "不可比"
        suffix = "（部分可比）" if partial else ""
        return f"+{value:,}{suffix}"

    flows = aggregate_underlying_flows(snapshot, previous, top_n=10)
    rank_lines = ["**标的热度 Top10**"]
    for rank, flow in enumerate(flows, start=1):
        held_mark = "📌 " if flow.underlying in held_underlyings else ""
        asset_type = " · ETF" if flow.underlying in etf_underlyings else ""
        rank_lines.append(f"#{rank} {held_mark}{flow.underlying}{asset_type}")
        rank_lines.append(
            f"Call {flow.call_volume:,} / Put {flow.put_volume:,} "
            f"· 总量 {flow.total_volume:,}"
        )
        call_put_ratio = flow.call_put_ratio
        ratio_text = (
            f"{call_put_ratio:.2f}" if call_put_ratio is not None else "-"
        )
        dominance_text = (
            f" {flow.dominance:.0%}" if flow.dominance is not None else ""
        )
        rank_lines.append(
            f"可见量 Call/Put 比 {ratio_text} · "
            f"{flow.structure_label}{dominance_text}"
        )
        if previous is None:
            rank_lines.append("首次可见，无 15 分钟可比增量")
        else:
            partial = flow.call_delta_partial or flow.put_delta_partial
            prefix = "15 分钟已知增量" if partial else "15 分钟增量"
            rank_lines.append(
                f"{prefix} Call "
                f"{delta_text(flow.known_call_delta, flow.call_delta_partial)} / "
                f"Put {delta_text(flow.known_put_delta, flow.put_delta_partial)}"
            )
    ranking = CardSection("\n".join(rank_lines))

    boundary = CardSection(
        "**解释边界**\n"
        "可见量来自 Cboe 四市场榜单样本，不是全 OPRA 总量。\n"
        "Call/Put 占优只表示成交量结构；Call成交不等于看涨，"
        "Put成交不等于看跌，也不能证明新开仓或主动买卖。\n"
        "Volume/OI使用前一结算日OI，不能证明新开仓；Indicative报价不能用于判断主动买卖。\n"
        "> 期权成交热度研究提醒，不自动下单，仅供观察，不构成投资建议。"
    )
    sections = tuple(
        section
        for section in (identity, focus, holding_section, ranking, boundary)
        if section is not None
    )
    body = "\n\n".join(section.content_md for section in sections)
    return Card(
        kind=(
            CardKind.SIGNAL
            if phase in ("change", "holding_change")
            else CardKind.REPORT
        ),
        title="🔥 美股期权热度 · Cboe四市场",
        body_md=body,
        sections=sections,
    )


def option_intel_card(
    intels: "Sequence[OptionIntel]",
    *,
    session: "date",
    iv_rv_warn_ratio: float = 1.5,
) -> Card:
    """持仓期权情报：期权市场定价作为持股决策语境，只观察不交易。"""

    def pct(value: float | None) -> str:
        return f"{value:.0%}" if value is not None else "-"

    def strike_text(value: Decimal) -> str:
        return format(value.normalize(), "f")

    technical_labels = {
        "OVERSOLD_AT_LOWER_BAND": "超卖并触及布林下轨",
        "MEAN_REVERSION_WATCH": "均值回归观察",
        "NEUTRAL": "中性",
        "DOWNTREND_CONTINUATION_RISK": "下跌中继风险",
        "DATA_INSUFFICIENT": "历史不足",
    }
    reliability_labels = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}
    conclusion_labels = {
        "TACTICAL_REBOUND": "战术反弹",
        "WAIT_REVERSAL_CONFIRMATION": "等待止跌确认",
        "PUT_WALL_BREAK_RISK": "Put墙破位风险",
        "FALLING_KNIFE_RISK": "下跌中继，避免抄底",
        "TECHNICAL_ONLY": "仅技术面观察",
        "DATA_INSUFFICIENT": "数据不足",
    }
    opex_labels = {
        "NORMAL": "常规",
        "OPEX_WINDOW": "临近窗口",
        "OPEX_DAY": "到期日",
        "POST_OPEX_RESET": "到期后重构",
    }

    def price(value: object) -> str:
        if not isinstance(value, (str, int, float, Decimal)):
            return "-"
        return f"{float(value):,.2f}"

    sections: list[CardSection] = []
    for intel in intels:
        header = f"**{intel.symbol}** ${float(intel.spot):,.2f}"
        if intel.earnings_date is not None:
            days = (intel.earnings_date - intel.session).days
            if days >= 0:
                header += f" · 📅 {days}天后财报({intel.earnings_date:%m-%d})"
        if intel.data_note is not None:
            move = f"期权数据: {intel.data_note}"
        elif intel.expected_move_pct is not None and intel.expected_move_expiry is not None:
            move = (
                f"预期波动: ±{intel.expected_move_pct:.1%} "
                f"到 {intel.expected_move_expiry:%m-%d}"
            )
            if intel.earnings_move_pct is not None and intel.earnings_move_expiry is not None:
                move += (
                    f"｜跨财报 ±{intel.earnings_move_pct:.1%} "
                    f"到 {intel.earnings_move_expiry:%m-%d}"
                )
        else:
            move = "预期波动: -"

        iv_line = f"ATM IV {pct(intel.atm_iv)} vs 20日实际波动 {pct(intel.realized_vol_20d)}"
        if intel.atm_iv is not None and intel.realized_vol_20d:
            ratio = intel.atm_iv / intel.realized_vol_20d
            iv_line += f" ({ratio:.1f}x)"
            if ratio >= iv_rv_warn_ratio:
                iv_line += " ⚠IV偏高(事件定价)"

        pc_volume = (
            f"{intel.pc_volume_ratio:.2f}" if intel.pc_volume_ratio is not None else "-"
        )
        pc_oi = f"{intel.pc_oi_ratio:.2f}" if intel.pc_oi_ratio is not None else "-"
        pc_line = f"Put/Call: 量比 {pc_volume} · OI比 {pc_oi}"

        if intel.top_oi_strikes:
            oi_items = " ".join(
                f"{strike_text(item.strike)}{'C' if item.side == 'call' else 'P'}"
                f"({item.expiration:%m/%d})"
                for item in intel.top_oi_strikes
            )
            oi_line = f"大OI: {oi_items}"
        else:
            oi_line = "大OI: -"

        lines = [header, move]
        if intel.data_note is None:
            lines.extend((f"{iv_line}｜{pc_line}", oi_line))
        tactical = intel.tactical
        if tactical is not None:
            technical = tactical.technical
            options = tactical.options
            partial = " · 本周进行中" if technical.latest_week_partial else ""
            reversal = (
                "已止跌确认"
                if technical.reversal_confirmed
                else "未止跌确认"
                if technical.reversal_confirmed is not None
                else "止跌确认不可用"
            )
            lines.append(
                f"周线 RSI {price(technical.weekly_rsi)} · "
                f"布林下轨 {price(technical.bollinger_lower)} / "
                f"中轨 {price(technical.bollinger_mid)} · "
                f"{technical_labels[technical.state]} · {reversal}{partial}"
            )
            lines.append(
                f"Put墙 {price(options.put_wall)} / "
                f"Call墙 {price(options.call_wall)} / "
                f"Max Pain {price(options.max_pain)} · "
                f"可信度{reliability_labels[options.reliability]}"
            )
            gamma = (
                f"{options.gamma_pin_score:.0%}"
                if options.gamma_pin_score is not None
                else "-"
            )
            lines.append(
                f"Gamma集中度 {gamma} · 做市商净Gamma方向不可由公开OI判定｜"
                f"月度OPEX {opex_labels[tactical.opex.state]}"
            )
            targets = " / ".join(
                value
                for value in (
                    price(tactical.target_1) if tactical.target_1 is not None else "",
                    price(tactical.target_2) if tactical.target_2 is not None else "",
                )
                if value
            ) or "-"
            lines.append(
                f"结论：{conclusion_labels[tactical.conclusion]}｜"
                f"失效参考 {price(tactical.invalidation_price)}｜"
                f"目标 {targets}｜{tactical.holding_note}"
            )
        sections.append(CardSection("\n".join(lines)))

    boundary = CardSection(
        "**解释边界**\n"
        "数据为 Alpaca indicative feed（约15分钟延迟，非 OPRA NBBO）。\n"
        "预期波动是期权市场定价的隐含区间，不是预测；Put/Call 高低不等于看空/看多\n"
        "（可能是对冲、平仓或价差）。大OI行权价仅为市场仓位参考。\n"
        "> 仅供观察，不构成投资建议；本系统不推荐任何期权交易。"
    )
    all_sections = (*sections, boundary)
    body = "\n\n".join(section.content_md for section in all_sections)
    return Card(
        kind=CardKind.REPORT,
        title=f"🧭 持仓期权情报 · {session:%Y-%m-%d}",
        body_md=body,
        sections=all_sections,
    )


def holding_price_alert_card(signal: Signal) -> Card:
    """真实持仓异动卡：只呈现可核验的价格/成交量观测，不转译成交易指令。"""

    extra = signal.extra or {}

    def percent(value: object) -> str:
        return f"{float(value):+.2%}" if isinstance(value, (int, float)) else "-"

    def price(value: object) -> str:
        return f"${float(value):,.2f}" if isinstance(value, (int, float)) else "-"

    raw_severity = extra.get("severity", 1)
    severity = int(raw_severity) if isinstance(raw_severity, (int, float)) else 1
    direction = "上涨" if signal.direction == Direction.BUY else "下跌"
    observed_raw = extra.get("observed_at")
    try:
        observed = datetime.fromisoformat(str(observed_raw)).astimezone(_ET).strftime(
            "%m/%d %H:%M:%S ET"
        )
    except ValueError:
        observed = str(observed_raw or "-")
    volume_ratio = extra.get("volume_ratio")
    volume_text = (
        f"{float(volume_ratio):.1f}倍" if isinstance(volume_ratio, (int, float)) else "-"
    )
    quantity = extra.get("quantity")
    quantity_text = f"{float(quantity):g}" if isinstance(quantity, (int, float)) else "-"

    raw_move = extra.get("move_pct", 0.0)
    move = float(raw_move) if isinstance(raw_move, (int, float)) else 0.0
    identity = CardSection(
        "**异动摘要**\n"
        f"{signal.ticker} · {extra.get('asset_type', '个股')} · {extra.get('window', '-')}"
        f"{direction} {abs(move):.2%}\n"
        f"现价 {price(signal.price)} · 强度 {severity}/3 · "
        f"自适应门槛 {percent(extra.get('threshold_pct'))}"
    )
    context = CardSection(
        "**实时窗口**\n"
        f"1分钟 {percent(extra.get('one_minute_pct'))} · "
        f"5分钟 {percent(extra.get('five_minute_pct'))} · "
        f"15分钟 {percent(extra.get('fifteen_minute_pct'))}\n"
        f"当日 {percent(extra.get('session_pct'))} · 末根分钟成交量/近20根中位数 {volume_text}"
    )
    cause_section: CardSection | None = None
    raw_cause = extra.get("price_move_cause")
    if isinstance(raw_cause, Mapping):
        category_labels = {
            "company_news": "公司消息",
            "sector": "行业联动",
            "macro": "宏观/市场",
            "technical": "技术/订单流",
            "unconfirmed": "原因未确认",
        }
        confidence_labels = {"high": "高", "medium": "中", "low": "低"}
        status_labels = {
            "ok": "搜索完成",
            "no_evidence": "未找到可核验证据",
            "disabled": "功能未启用",
            "timeout": "搜索超时",
            "failed": "搜索失败",
            "invalid_output": "返回结果未通过校验",
        }
        category = category_labels.get(
            str(raw_cause.get("category", "unconfirmed")), "原因未确认"
        )
        confidence = confidence_labels.get(
            str(raw_cause.get("confidence", "low")), "低"
        )
        status = status_labels.get(
            str(raw_cause.get("search_status", "invalid_output")), "查因异常"
        )
        cause_lines = [
            "**Codex实时查因**",
            f"{category} · 置信度{confidence} · {status}",
            str(raw_cause.get("summary") or "暂无可核验原因。"),
        ]
        raw_evidence = raw_cause.get("evidence")
        if isinstance(raw_evidence, list):
            for index, item in enumerate(raw_evidence[:3], start=1):
                if not isinstance(item, Mapping):
                    continue
                source = str(item.get("source") or "来源")
                title = str(item.get("title") or "查看原文")
                url = str(item.get("url") or "")
                published = str(item.get("published_at") or "时间未标注")
                if url.startswith(("https://", "http://")):
                    cause_lines.append(
                        f"• [来源{index}]({url}) · {source} · {title} · {published}"
                    )
        elapsed = raw_cause.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            cause_lines.append(f"搜索耗时 {float(elapsed):.1f}秒")
        cause_lines.append("相关性不等于因果；结论会随新消息更新。")
        cause_section = CardSection("\n".join(cause_lines))
    holding = CardSection(
        "**持仓语境**\n"
        f"数量 {quantity_text} · 成本 {price(extra.get('avg_entry_price'))} · "
        f"相对成本 {percent(extra.get('pnl_from_cost_pct'))}\n"
        "本提醒用于实时观察，不代表自动买入、卖出或调仓。"
    )
    data = CardSection(
        "**数据身份**\n"
        f"{extra.get('data_feed', 'unknown')} · {observed}\n"
        "分钟线可能因行情源权限、延迟或修正而变化。"
    )
    sections = tuple(
        section
        for section in (identity, context, cause_section, holding, data)
        if section is not None
    )
    return Card(
        kind=CardKind.ALERT,
        title=f"⚡ 持仓股价异动 · {signal.ticker} · {severity}级",
        body_md="\n\n".join(section.content_md for section in sections),
        sections=sections,
    )


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
    if s.direction == Direction.SELL:
        lines.append(_SELL_RELIABILITY_NOTE)
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
_ADVISORY_FOOTER = "> 观察模式，不自动下单。仅供观察，不构成投资建议。"


def us_briefing_card(
    *,
    report_kind: str,
    as_of: str,
    regime: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    discipline: Sequence[Mapping[str, object]],
    portfolio_risk: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
    data_quality: Sequence[str],
    ai_summary: str | None = None,
    company_rationales: Mapping[str, str] | None = None,
    news_lookback_days: int = 7,
) -> Card:
    """Render a mobile-first US briefing without wide Markdown tables."""
    regime_code = str(regime.get("regime", "UNKNOWN"))
    regime_names = {
        "TREND": "趋势",
        "PULLBACK": "回调",
        "RANGE": "震荡",
        "RISK_OFF": "风险关闭",
        "UNKNOWN": "数据不足",
    }
    kind_names = {
        "US_CLOSE": "美股收盘简报",
        "ASIA_CONFIRM": "亚洲确认与美股计划",
        "DAILY_ACTION": "今日美股行动简报",
    }
    reasons = regime.get("reasons")
    reason_text = "、".join(str(item) for item in reasons) if isinstance(reasons, list) else "-"
    sections: list[CardSection] = [
        CardSection(
            "**今日结论**\n"
            f"市场状态：{regime_names.get(regime_code, regime_code)}\n"
            f"50日线上宽度 {_briefing_percent(regime.get('breadth_above_50d'))}"
            f" · 实现波动 {_briefing_percent(regime.get('realized_volatility'))}\n"
            f"依据：{reason_text}"
        )
    ]
    lane_names = {
        "TREND_CONTINUATION": "趋势延续",
        "TREND_PULLBACK": "强势回调",
        "RANGE_REVERSION": "震荡修复",
    }
    sector_grouped = any(
        candidate.get("candidate_group") or candidate.get("gics_sector")
        for candidate in candidates
    )
    lane_lines = [
        "**行业分组候选 · 市值门槛 ≥ 1000亿美元**"
        if sector_grouped
        else "**纳指100候选**"
    ]
    if not candidates:
        lane_lines.append("当前状态没有满足条件的新多仓；不为凑榜而交易。")
    current_sector: str | None = None
    for candidate in candidates:
        ticker = str(candidate.get("ticker", "-"))
        sector = str(
            candidate.get("candidate_group") or candidate.get("gics_sector", "")
        )
        if sector_grouped and sector != current_sector:
            current_sector = sector
            lane_lines.append(f"\n**{_sector_name_zh(sector)}**")
        lane = lane_names.get(
            str(candidate.get("lane", "")), str(candidate.get("lane", "-"))
        )
        quantity = candidate.get("suggested_qty")
        block_reason = candidate.get("block_reason")
        company = str(candidate.get("company_name") or "")
        label = f"{ticker} · {company}" if company else ticker
        if block_reason:
            reason_zh = _BLOCK_REASON_NAMES_ZH.get(str(block_reason), str(block_reason))
            lane_lines.append(f"• {label}｜{lane}｜仅观察：{reason_zh}")
        else:
            sizing = (
                f"｜建议 {_briefing_number(quantity, 0)} 股 / "
                f"${_briefing_number(candidate.get('suggested_notional'))}"
                if quantity is not None
                else "｜股数不可用（账户数据不足）"
            )
            lane_lines.append(
                f"• {label}｜{lane}｜买入 {_briefing_number(candidate.get('entry_low'))}"
                f"–{_briefing_number(candidate.get('entry_high'))}｜失效 "
                f"{_briefing_number(candidate.get('invalidation_price'))}{sizing}"
            )
            raw_targets = candidate.get("profit_targets")
            if isinstance(raw_targets, (list, tuple)) and len(raw_targets) == 3:
                lane_lines.append(
                    f"  止盈1 {_briefing_number(raw_targets[0])} · "
                    f"止盈2 {_briefing_number(raw_targets[1])} · "
                    f"止盈3 {_briefing_number(raw_targets[2])} · "
                    f"建议第{candidate.get('recommended_target_stage', 1)}档"
                )
                lane_lines.append(
                    "  近5日买盘资金估算 "
                    f"{_usd_yi(candidate.get('recent_buying_notional'))} · "
                    f"强度{candidate.get('buying_pressure_label') or '-'} "
                    f"{_briefing_percent(candidate.get('buying_pressure_score'))}"
                )
                if candidate.get("nearby_resistance") is not None:
                    lane_lines.append(
                        f"  近期阻力 {_briefing_number(candidate.get('nearby_resistance'))}"
                    )
            else:
                lane_lines.append(
                    f"  单一止盈参考 {_briefing_number(candidate.get('target_price'))}"
                )
        if sector_grouped:
            lane_lines.append(
                f"  策略 #{candidate.get('sector_strategy_rank', '-')} · "
                f"合格同行市值 #{candidate.get('sector_market_cap_rank', '-')} · "
                f"市值 {_market_cap_zh(candidate.get('market_cap_usd'))}"
            )
            lane_lines.append(
                f"  {_industry_name_zh(candidate.get('industry'))} · "
                f"画像 {candidate.get('profile_as_of') or '-'} · "
                f"新闻窗口近{news_lookback_days}日"
            )
            rationale = (company_rationales or {}).get(ticker)
            if rationale:
                lane_lines.extend(f"  {line}" for line in rationale.splitlines())
    sections.append(CardSection("\n".join(lane_lines)))
    discipline_lines = ["**持仓纪律**"]
    if not discipline:
        discipline_lines.append("没有可用持仓，或账户数据不足。")
    for item in discipline:
        ticker = str(item.get("ticker", "-"))
        status = str(item.get("status", "HOLD"))
        quantity = item.get("incremental_sell_qty")
        quantity_number = _briefing_float(quantity)
        incremental_number = _briefing_float(item.get("incremental_sell_fraction"))
        pending_number = _briefing_float(item.get("pending_sell_fraction"))
        fraction = _briefing_percent(item.get("incremental_sell_fraction"))
        cumulative = _briefing_percent(item.get("cumulative_sell_fraction"))
        if status == "EXIT_DUE":
            action = "止损/利润保护退出条件已触发"
        elif quantity_number is not None and quantity_number > 0:
            action = f"卖出 {_briefing_number(quantity, 0)} 股（累计 {cumulative}）"
        elif incremental_number is not None and incremental_number > 0:
            action = f"卖出 {fraction}（股数不可用）"
        elif status == "TAKE_PROFIT_DUE" and pending_number is not None and pending_number > 0:
            action = f"止盈仍待执行（累计应减 {cumulative}）"
        elif status == "REDUCE":
            action = "降低有效敞口"
        else:
            action = "继续持有/观察"
        cost_label = "成本估算" if item.get("cost_quality") == "ESTIMATED" else "成本"
        discipline_lines.append(
            f"• {ticker}｜{action}｜{cost_label} {_briefing_number(item.get('cost_basis'))}"
            f"｜保护价 {_briefing_number(item.get('protection_price'))}"
        )
    sections.append(CardSection("\n".join(discipline_lines)))
    risk_lines = ["**杠杆与组合风险**"]
    risk_lines.append(
        f"总有效仓位 {_briefing_percent(portfolio_risk.get('total_effective_weight'))}"
        f" · 杠杆有效仓位 {_briefing_percent(portfolio_risk.get('leveraged_effective_weight'))}"
    )
    risk_warnings = portfolio_risk.get("warnings")
    if isinstance(risk_warnings, (list, tuple)) and risk_warnings:
        risk_lines.append("提醒：" + "、".join(str(item) for item in risk_warnings))
    else:
        risk_lines.append("当前没有新增杠杆风险升级。")
    sections.append(CardSection("\n".join(risk_lines)))
    observation_lines = ["**观察与数据边界**"]
    observation_labels = {
        "INSUFFICIENT_HISTORY": "历史不足",
        "OVERHEATED": "过热不追",
        "LIQUIDITY_FILTER": "流动性过滤",
        "EARNINGS_WINDOW": "财报窗口",
        "CLUSTER_CAP": "主题集中限制",
        "INVALID_VOLATILITY": "波动数据异常",
        "PROFILE_UNAVAILABLE": "公司画像不可用",
        "NON_EQUITY": "非公司股票",
        "MARKET_CAP_FILTER": "市值低于1000亿美元",
        "SECTOR_CAP": "行业Top3限制",
        "SECTOR_NOT_SELECTED": "行业数量限制",
    }
    observation_counts = Counter(
        str(item.get("reason", "仅观察")) for item in observations
    )
    if observation_counts:
        observation_lines.append(
            "筛除汇总："
            + " · ".join(
                f"{observation_labels.get(reason, reason)} {count}"
                for reason, count in sorted(observation_counts.items())
            )
        )
    notable = [
        item
        for item in observations
        if item.get("ticker") == "SKHY" or item.get("reason") == "EARNINGS_WINDOW"
    ][:4]
    for item in notable:
        if item.get("reason") == "INSUFFICIENT_HISTORY":
            observation_lines.append(
                f"• {item.get('ticker')}：历史仅 {item.get('history_days', 0)} 个交易日，暂不排名。"
            )
        else:
            observation_lines.append(
                f"• {item.get('ticker')}：{item.get('reason', '仅观察')}"
            )
    observation_lines.extend(f"• {item}" for item in data_quality)
    sections.append(CardSection("\n".join(observation_lines)))
    if ai_summary:
        sections.append(CardSection("**AI解释（不改写数值）**\n" + ai_summary.strip()[:500]))
    sections.append(
        CardSection(
            "> 价格触发不保证成交，隔夜跳空可能穿价；系统不自动下单。仅供观察，不构成投资建议。"
        )
    )
    body = "\n\n".join(section.content_md for section in sections)
    return Card(
        kind=CardKind.REPORT,
        title=f"📌 {kind_names.get(report_kind, report_kind)} · {as_of}",
        body_md=body,
        sections=tuple(sections),
    )


def us_briefing_cards(
    *,
    report_kind: str,
    as_of: str,
    regime: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    discipline: Sequence[Mapping[str, object]],
    portfolio_risk: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
    data_quality: Sequence[str],
    ai_summary: str | None = None,
    company_rationales: Mapping[str, str] | None = None,
    news_lookback_days: int = 7,
) -> tuple[Card, ...]:
    """Split the daily action summary from verbose per-sector candidates."""
    summary = us_briefing_card(
        report_kind=report_kind,
        as_of=as_of,
        regime=regime,
        candidates=(),
        discipline=discipline,
        portfolio_risk=portfolio_risk,
        observations=observations,
        data_quality=data_quality,
        ai_summary=ai_summary,
        company_rationales=None,
        news_lookback_days=news_lookback_days,
    )
    summary_sections = tuple(
        section
        for section in summary.sections
        if not section.content_md.startswith(("**行业分组候选", "**纳指100候选"))
    )
    summary = Card(
        kind=summary.kind,
        title=summary.title,
        body_md="\n\n".join(section.content_md for section in summary_sections),
        url=summary.url,
        sections=summary_sections,
    )

    grouped: dict[str, list[Mapping[str, object]]] = {}
    for candidate in candidates:
        group = str(
            candidate.get("candidate_group")
            or candidate.get("gics_sector")
            or "纳指100"
        )
        grouped.setdefault(group, []).append(candidate)

    output: list[Card] = [summary]
    for group, rows in grouped.items():
        rationales = {
            str(row.get("ticker")): (company_rationales or {})[str(row.get("ticker"))]
            for row in rows
            if str(row.get("ticker")) in (company_rationales or {})
        }
        rendered = us_briefing_card(
            report_kind=report_kind,
            as_of=as_of,
            regime=regime,
            candidates=rows,
            discipline=(),
            portfolio_risk={},
            observations=(),
            data_quality=(),
            company_rationales=rationales,
            news_lookback_days=news_lookback_days,
        )
        candidate_section = next(
            section
            for section in rendered.sections
            if section.content_md.startswith(("**行业分组候选", "**纳指100候选"))
        )
        boundary = CardSection(
            "> 买盘资金为日线 OHLCV 估算，不是逐笔净流入；系统不自动下单。"
        )
        sections = (candidate_section, boundary)
        output.append(
            Card(
                kind=CardKind.REPORT,
                title=f"📈 {_sector_name_zh(group)}候选 · {as_of}",
                body_md="\n\n".join(section.content_md for section in sections),
                sections=sections,
            )
        )
    return tuple(output)


def _fmt_qty(value: object) -> str:
    return str(value) if value is not None else "-"


def _fmt_price(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def execution_plan_card(
    account: "AccountState | None",
    plans: "Sequence[ExecutionPlan]",
    now: datetime,
    *,
    ai_summary: str | None = None,
    account_warning: str | None = None,
) -> Card:
    def money(value: Decimal | float | int | None) -> str:
        return "-" if value is None else f"${float(value):,.2f}"

    source_label = "PAPER"
    held: set[str] = set()
    sections: list[CardSection] = []
    if account is None:
        lines = ["**账户**", "⚠ 账户数据不足，未计算股数；仅展示观察价位。"]
        if account_warning:
            lines.append(account_warning)
        sections.append(CardSection("\n".join(lines)))
    else:
        snap = account.snapshot
        source_label = "截图账户" if snap.source == "screenshot" else "PAPER"
        held = {position.symbol for position in account.positions} | {
            position.symbol for position in account.observed_positions
        }
        account_time = snap.retrieved_at.astimezone(_SGT).strftime("%Y-%m-%d %H:%M")
        limit = snap.capital_limit or snap.equity
        financing = limit * snap.max_financing_ratio
        market_value = snap.market_value
        if market_value is None:
            market_value = sum(
                (position.market_value for position in account.positions), Decimal("0")
            )
        weight = float(market_value / snap.equity) if snap.equity else 0.0
        count = account.reported_position_count
        if count is None:
            count = len(held)
        lines = [
            "**账户**",
            f"净值 {money(snap.equity)}｜资金上限 {money(limit)}",
            f"现金 {money(snap.cash)}｜持仓 {count}｜当前仓位 {weight:.1%}",
            f"融资上限 {money(financing)}｜总敞口≤{money(snap.max_gross_exposure)}",
            f"数据来源：{source_label} · {account_time} SGT",
        ]
        if account.positions:
            position_text = "；".join(
                f"{position.symbol} {position.qty}股/市值{money(position.market_value)}"
                for position in account.positions[:3]
            )
            lines.append(f"持仓摘要：{position_text}")
        if account.open_orders:
            order_text = "；".join(
                f"{order.symbol} {order.side.upper()} {order.qty} @ {order.limit_price or '市价'}"
                for order in account.open_orders[:3]
            )
            lines.append(f"未成交：{order_text}")
        if account.recent_orders:
            recent_text = "；".join(
                f"{order.order_id} {order.symbol} {order.side.upper()} {order.filled_qty} @ {order.filled_avg_price or '-'}"
                for order in account.recent_orders[:3]
            )
            lines.append(f"最近成交：{recent_text}")
        if account_warning:
            lines.append(account_warning)
        sections.append(CardSection("\n".join(lines)))

    actionable = [plan for plan in plans if plan.state.value != "BLOCKED"]
    blocked = [plan for plan in plans if plan.state.value == "BLOCKED"]
    primary = actionable[:3]
    alternates = actionable[3:5]
    sections.append(
        CardSection(
            "**今日结论**\n"
            f"可执行/等待 {len(actionable)}｜暂不交易 {len(blocked)}\n"
            "未满足完整5分钟K确认时，不买、不追价。"
        )
    )

    state_names = {
        "CANDIDATE": "等待触发",
        "ARMED": "等待回落",
        "IN_ENTRY_ZONE": "进入观察区",
        "ACTIONABLE": "条件已确认",
        "AWAITING_FILL": "等待成交确认",
    }

    def plan_block(item: "ExecutionPlan") -> str:
        expiry = item.expires_at.astimezone(_ET).strftime("%H:%M ET")
        ownership = "已有持仓" if item.ticker in held else "未持仓新买候选"
        qty = f"最多 {item.suggested_qty} 股" if item.suggested_qty is not None else "数量不可用"
        return (
            f"**{item.ticker} · {ownership} · {state_names.get(item.state.value, item.state.value)}**\n"
            f"条件：09:45 ET 后进入 {item.currency} {item.entry_low:.2f}–{item.entry_high:.2f}，"
            "且完整5分钟K确认趋势有效\n"
            f"限价：不高于 {item.currency} {item.limit_price:.2f}｜{qty}｜约 {money(item.suggested_notional)}\n"
            f"风控：止损 {item.stop_loss:.2f}｜止盈 {item.take_profit:.2f}｜有效至 {expiry}"
        )

    if primary:
        sections.append(CardSection("**今天可做**\n\n" + "\n\n".join(plan_block(item) for item in primary)))
    if alternates:
        sections.append(CardSection("**次选观察**\n" + "\n".join(
            f"{item.ticker} · {item.currency} {item.entry_low:.2f}–{item.entry_high:.2f} · 最多 {_fmt_qty(item.suggested_qty)}股"
            for item in alternates
        )))

    if account is not None:
        holding_lines = ["**持仓风险**"]
        if account.positions_partial:
            holding_lines.append("⚠ 持仓明细不完整：当前只按市值敞口控风险，不输出卖出股数。")
        weighted = sorted(
            (
                position
                for position in account.observed_positions
                if position.weight_pct is not None
            ),
            key=lambda position: position.weight_pct or Decimal("0"),
            reverse=True,
        )[:3]
        for position in weighted:
            holding_lines.append(
                f"{position.symbol}：仓位 {float(position.weight_pct or 0):.2f}%"
                + (
                    f"｜持仓盈亏 {float(position.pnl_pct):+.2f}%"
                    if position.pnl_pct is not None
                    else ""
                )
            )
        if len(holding_lines) > 1:
            sections.append(CardSection("\n".join(holding_lines)))

    if blocked:
        reason_names = {
            "MAX_NEW_POSITIONS": "新仓数量上限",
            "STOP_TOO_TIGHT": "止损过窄",
            "STALE_ACCOUNT": "账户过期",
            "STALE_QUOTE": "行情过期",
            "CLUSTER_WEIGHT_EXCEEDED": "主题集中度超限",
            "UNSUPPORTED_MARKET": "不支持的市场/币种",
            "POSITION_QTY_UNKNOWN": "持仓数量缺失",
            "PORTFOLIO_BUDGET_EXHAUSTED": "资金预算已用完",
            "DAILY_RISK_EXHAUSTED": "当日风险预算已用完",
            "DATA_STALE": "数据过期",
        }
        grouped: dict[str, list["ExecutionPlan"]] = defaultdict(list)
        for item in blocked:
            grouped[item.block_reason or "UNKNOWN"].append(item)
        counts = Counter(item.block_reason or "UNKNOWN" for item in blocked)
        lines = ["**暂不交易**"]
        for reason, count in counts.items():
            tickers = "、".join(
                f"{item.ticker}({item.currency})" for item in grouped[reason][:3]
            )
            lines.append(f"{reason_names.get(reason, reason)}（{reason}）{count}：{tickers}")
        sections.append(CardSection("\n".join(lines)))

    if ai_summary:
        sections.append(CardSection("**AI简评**\n" + ai_summary.strip()[:300]))
    footer = (
        "> 截图账户/观察模式，不自动下单。仅供观察，不构成投资建议。"
        if source_label == "截图账户"
        else _PAPER_FOOTER
    )
    sections.append(CardSection(footer))
    body = "\n\n".join(section.content_md for section in sections)
    title = (
        f"🧭 今日行动计划 · {now.astimezone(_ET):%m/%d}"
        if source_label == "截图账户"
        else f"🧭 PAPER 今日行动计划 · {now.astimezone(_ET):%m/%d}"
    )
    return Card(
        kind=CardKind.REPORT,
        title=title,
        body_md=body,
        sections=tuple(sections),
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
        _ADVISORY_FOOTER,
    ]
    return Card(
        kind=CardKind.SIGNAL,
        title=f"⚡ 执行提醒 · {plan.ticker} {event}",
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
    if any(row["direction"] == Direction.SELL.value for row in rows):
        lines.extend(["", _SELL_RELIABILITY_NOTE])
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
    if any(signal.direction == Direction.SELL for signal in signals):
        parts.append(_SELL_RELIABILITY_NOTE)
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
