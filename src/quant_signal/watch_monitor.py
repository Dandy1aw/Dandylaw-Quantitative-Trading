"""持仓/今日目标标的盘中偏离监控：相对当天参考价涨跌超阈值即提醒。

纯函数，不触碰数据库或网络——Engine 负责取参考价/实时价后调用本模块。
"""

from __future__ import annotations

from datetime import datetime

from quant_signal.strategies.base import Direction, Signal

STRATEGY_ID = "price_deviation"
TARGET_HIT_STRATEGY_ID = "target_hit"


def check_target_hits(
    targets: dict[str, float],
    live_prices: dict[str, float],
    now: datetime,
    tolerance: float = 0.002,
) -> list[Signal]:
    """到价提醒：近几日 BUY 信号的目标买入价被实时价触及(≤目标×(1+容差))即提醒。
    去重靠默认键(ticker|buy|target_hit)+4h窗口，同一目标不会反复轰炸。"""
    signals: list[Signal] = []
    for ticker, target in targets.items():
        live = live_prices.get(ticker)
        if live is None or target <= 0:
            continue
        if live <= target * (1.0 + tolerance):
            signals.append(
                Signal(
                    ticker=ticker,
                    direction=Direction.BUY,
                    price=live,
                    reason=f"到价提醒：目标买入价 {target:.2f}，现价 {live:.2f} 已进入买区",
                    strategy_id=TARGET_HIT_STRATEGY_ID,
                    ts=now,
                    extra={"target_buy": target},
                )
            )
    return signals


def check_deviations(
    ref_prices: dict[str, float],
    live_prices: dict[str, float],
    now: datetime,
    threshold: float = 0.02,
    bands: list[float] | None = None,
) -> list[Signal]:
    """偏离超阈值即提醒。bands 为升级档位(升序，最小档=入场阈值)；不传则退化为
    单档 [threshold]。破到更高档会生成不同的去重键(dedup_suffix)，从而突破 4h 去重
    窗口再推一次"升级告警"；同档内则仍被压制，不重复轰炸。"""
    tiers = sorted(bands) if bands else [threshold]
    entry = tiers[0]
    signals: list[Signal] = []
    for ticker, ref in ref_prices.items():
        live = live_prices.get(ticker)
        if live is None:
            continue
        pct = live / ref - 1.0
        apct = abs(pct)
        if apct <= entry:
            continue
        band_idx = max(i for i, b in enumerate(tiers) if b <= apct)
        tier = tiers[band_idx]
        direction = Direction.BUY if pct > 0 else Direction.SELL
        verb = "上涨" if pct > 0 else "下跌"
        signals.append(
            Signal(
                ticker=ticker,
                direction=direction,
                price=live,
                reason=(
                    f"相对参考价 {ref:.2f} {verb} {apct:.1%}"
                    f"（破 {tier:.0%} 档），现价 {live:.2f}"
                ),
                strategy_id=STRATEGY_ID,
                ts=now,
                extra={"ref_price": ref, "pct_change": pct, "band": tier},
                dedup_suffix=f"b{band_idx}",
            )
        )
    return signals
