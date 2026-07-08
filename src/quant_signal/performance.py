"""信号绩效闭环（P2）：从 signals 台账重放推导虚拟往返，出策略级周报。

虚拟盘规则：对每个 (策略, 标的) 按时间顺序重放**已推送**信号——空仓时 BUY 即
按信号价开仓，持仓时 SELL 即按信号价平仓；持仓中的重复 BUY / 空仓时的 SELL
忽略（去重窗口的再推送）。未平仓头寸按最新收盘 mark。

诚实口径：按信号价成交、不计滑点/手续费；这是检验"哪个策略在贡献"的相对
度量，不是可实现收益的承诺。price_deviation 等告警型策略不是交易意图，排除。
"""

from __future__ import annotations

from dataclasses import dataclass

from quant_signal.notifier.base import Card, CardKind

# 告警型策略：信号是"提醒"不是"下单意图"，不入虚拟盘
_NON_TRADING = {"price_deviation", "target_hit"}

_LABELS = {
    "momentum_rotation": "动量轮动",
    "breakout_20d": "20日突破",
    "macd_cross": "MACD",
    "rsi_reversion": "RSI回归",
    "bollinger_breakout": "布林带",
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


def build_round_trips(rows: list[dict[str, object]]) -> list[Trade]:
    """rows: 已推送信号(按 pushed_at 升序)，字段含 strategy_id/ticker/direction/price/pushed_at。"""
    open_pos: dict[tuple[str, str], Trade] = {}
    done: list[Trade] = []
    for row in rows:
        sid = str(row["strategy_id"])
        if sid in _NON_TRADING:
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
) -> Card:
    lines = [
        f"近 {window_days} 天已推送信号的虚拟盘复盘（按信号价成交，不计滑点/费用）",
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
    return Card(kind=CardKind.REPORT, title="📈 策略绩效周报", body_md="\n".join(lines))
