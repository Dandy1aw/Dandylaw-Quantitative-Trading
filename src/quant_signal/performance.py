"""信号绩效闭环（P2）：从 signals 台账重放推导虚拟往返，出策略级周报。

虚拟盘规则：对每个 (策略, 标的) 按时间顺序重放**已推送**信号——空仓时 BUY 即
按信号价开仓，持仓时 SELL 即按信号价平仓；持仓中的重复 BUY / 空仓时的 SELL
忽略（去重窗口的再推送）。未平仓头寸按最新收盘 mark。

诚实口径：按信号价成交、不计滑点/手续费；这是检验"哪个策略在贡献"的相对
度量，不是可实现收益的承诺。price_deviation 等告警型策略不是交易意图，排除。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math

import pandas as pd

from quant_signal.notifier.base import Card, CardKind

# 告警型策略：信号是"提醒"不是"下单意图"，不入虚拟盘
_NON_TRADING = {"price_deviation", "target_hit"}
_HORIZON_TRADING = {"market_scan"}

_LABELS = {
    "momentum_rotation": "动量轮动",
    "breakout_20d": "20日突破",
    "macd_cross": "MACD",
    "rsi_reversion": "RSI回归",
    "bollinger_breakout": "布林带",
    "market_scan": "全市场扫描",
}


@dataclass(frozen=True)
class Trade:
    strategy_id: str
    ticker: str
    entry_price: float
    entry_at: str
    exit_price: float | None = None
    exit_at: str | None = None

    @property
    def closed(self) -> bool:
        return self.exit_price is not None


@dataclass(frozen=True)
class FillPerformance:
    source: str
    currency: str
    realized_pnl: Decimal
    matched_cost: Decimal
    matched_quantity: Decimal
    buy_orders: int
    sell_orders: int
    unmatched_sell_quantity: Decimal

    @property
    def realized_return(self) -> Decimal:
        return (
            self.realized_pnl / self.matched_cost
            if self.matched_cost > 0
            else Decimal("0")
        )

    @property
    def is_live_source(self) -> bool:
        return self.source not in {"alpaca_paper", "screenshot", "simulation"}


def broker_fill_performance(
    rows: list[dict[str, object]],
) -> list[FillPerformance]:
    """FIFO realized gross P&L from durable broker-reported aggregate fills.

    Fees are not present in the current account API model, so this is never
    labelled net performance. Unmatched sells are surfaced instead of silently
    inventing a cost basis.
    """
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (str(row.get("source") or "unknown"), str(row.get("currency") or "USD"))
        grouped.setdefault(key, []).append(row)

    summaries: list[FillPerformance] = []
    for (source, currency), fills in sorted(grouped.items()):
        lots: dict[str, list[list[Decimal]]] = {}
        pnl = Decimal("0")
        cost = Decimal("0")
        matched = Decimal("0")
        unmatched = Decimal("0")
        buys = 0
        sells = 0
        for row in fills:
            try:
                qty = abs(Decimal(str(row["filled_qty"])))
                price = Decimal(str(row["filled_avg_price"]))
            except (KeyError, InvalidOperation, ValueError):
                continue
            if qty <= 0 or price <= 0:
                continue
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            if side == "buy":
                buys += 1
                lots.setdefault(symbol, []).append([qty, price])
                continue
            if side != "sell":
                continue
            sells += 1
            remaining = qty
            queue = lots.setdefault(symbol, [])
            while remaining > 0 and queue:
                lot_qty, lot_price = queue[0]
                closed_qty = min(remaining, lot_qty)
                matched += closed_qty
                cost += closed_qty * lot_price
                pnl += closed_qty * (price - lot_price)
                remaining -= closed_qty
                lot_qty -= closed_qty
                if lot_qty == 0:
                    queue.pop(0)
                else:
                    queue[0][0] = lot_qty
            unmatched += remaining
        summaries.append(
            FillPerformance(
                source=source,
                currency=currency,
                realized_pnl=pnl,
                matched_cost=cost,
                matched_quantity=matched,
                buy_orders=buys,
                sell_orders=sells,
                unmatched_sell_quantity=unmatched,
            )
        )
    return summaries


def build_round_trips(rows: list[dict[str, object]]) -> list[Trade]:
    """rows: 已推送信号(按 pushed_at 升序)，字段含 strategy_id/ticker/direction/price/pushed_at。"""
    open_pos: dict[tuple[str, str], Trade] = {}
    done: list[Trade] = []
    for row in rows:
        sid = str(row["strategy_id"])
        if sid in _NON_TRADING or sid in _HORIZON_TRADING:
            continue
        key = (sid, str(row["ticker"]))
        direction = str(row["direction"])
        price = float(row["price"])  # type: ignore[arg-type]
        at = str(row["pushed_at"])
        if direction == "buy" and key not in open_pos:
            open_pos[key] = Trade(sid, key[1], price, at)
        elif direction == "sell" and key in open_pos:
            pos = open_pos.pop(key)
            done.append(
                Trade(pos.strategy_id, pos.ticker, pos.entry_price, pos.entry_at, price, at)
            )
    return done + list(open_pos.values())


def build_horizon_trades(
    rows: list[dict[str, object]], bars: pd.DataFrame, horizon_days: int = 20
) -> list[Trade]:
    """扫描信号按下一交易日开盘入场，第 horizon_days 日收盘离场。"""
    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    if bars.empty:
        return []
    available = set(bars.index.get_level_values("ticker"))
    trades: list[Trade] = []
    for row in rows:
        if str(row["strategy_id"]) != "market_scan" or str(row["direction"]) != "buy":
            continue
        ticker = str(row["ticker"])
        if ticker not in available:
            continue
        signal_at = pd.Timestamp(str(row["pushed_at"]))
        if signal_at.tzinfo is None:
            signal_at = signal_at.tz_localize("UTC")
        else:
            signal_at = signal_at.tz_convert("UTC")
        signal_day = signal_at.normalize()
        sub = bars.xs(ticker, level="ticker").sort_index()
        idx = pd.DatetimeIndex(sub.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        future = sub.loc[idx.normalize() > signal_day]
        future = future[pd.to_numeric(future["open"], errors="coerce").map(math.isfinite)]
        if future.empty:
            continue
        entry_at = pd.Timestamp(future.index[0])
        entry_price = float(future["open"].iloc[0])
        if len(future) < horizon_days:
            trades.append(Trade("market_scan", ticker, entry_price, entry_at.isoformat()))
            continue
        exit_at = pd.Timestamp(future.index[horizon_days - 1])
        exit_price = float(future["close"].iloc[horizon_days - 1])
        if not math.isfinite(exit_price):
            continue
        trades.append(
            Trade(
                "market_scan",
                ticker,
                entry_price,
                entry_at.isoformat(),
                exit_price,
                exit_at.isoformat(),
            )
        )
    return trades


def strategy_summary(
    trades: list[Trade], marks: dict[str, float]
) -> dict[str, dict[str, float]]:
    """按策略聚合：已平仓笔数/胜率/平均收益 + 未平仓笔数/浮动平均收益(缺 mark 价的不计)。"""
    out: dict[str, dict[str, float]] = {}
    for sid in sorted({t.strategy_id for t in trades}):
        closed = [t for t in trades if t.strategy_id == sid and t.closed]
        open_trades = [t for t in trades if t.strategy_id == sid and not t.closed]
        rets = [t.exit_price / t.entry_price - 1.0 for t in closed if t.exit_price]
        wins = [r for r in rets if r > 0]
        open_rets = [
            marks[t.ticker] / t.entry_price - 1.0
            for t in open_trades
            if t.ticker in marks
        ]
        out[sid] = {
            "closed": len(closed),
            "win_rate": len(wins) / len(rets) if rets else 0.0,
            "avg_ret": sum(rets) / len(rets) if rets else 0.0,
            "open": len(open_trades),
            "open_avg_ret": sum(open_rets) / len(open_rets) if open_rets else 0.0,
        }
    return out


def performance_card(
    summary: dict[str, dict[str, float]],
    window_days: int,
    benchmark_note: str | None = None,
    fill_performance: list[FillPerformance] | None = None,
) -> Card:
    lines = [
        f"近 {window_days} 天已推送信号复盘（扫描=次日开盘持有20日；其余=信号价，不计费用）",
        "",
        "| 策略 | 已平仓 | 胜率 | 平均收益 | 未平仓 | 浮动均益 |",
        "|---|---|---|---|---|---|",
    ]
    for sid, s in summary.items():
        label = _LABELS.get(sid, sid)
        lines.append(
            f"| {label} | {s['closed']:.0f} | {s['win_rate']:.0%} | {s['avg_ret']:+.1%} |"
            f" {s['open']:.0f} | {s['open_avg_ret']:+.1%} |"
        )
    if not summary:
        lines.append("| (暂无已推送的交易信号) | - | - | - | - | - |")
    if benchmark_note:
        lines += ["", benchmark_note]
    if fill_performance:
        lines += ["", "**券商成交事实（FIFO 毛收益，未扣费用）**"]
        for item in fill_performance:
            mode = "券商回报" if item.is_live_source else "模拟/观察源"
            lines.append(
                f"- {item.source} · {mode}：已配对 {item.matched_quantity} 股，"
                f"已实现 {item.currency} {item.realized_pnl:+,.2f} "
                f"({item.realized_return:+.2%})"
            )
            if item.unmatched_sell_quantity > 0:
                lines.append(
                    f"  - {item.unmatched_sell_quantity} 股卖出缺少历史买入成本，未计入收益"
                )
    return Card(kind=CardKind.REPORT, title="📈 策略绩效周报", body_md="\n".join(lines))
