from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant_signal.config import HoldingPriceAlertSettings
from quant_signal.strategies.base import Direction, Signal

STRATEGY_ID = "holding_price_alert"
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class HoldingPriceAlertEvaluation:
    signals: tuple[Signal, ...]
    requested: int
    available: int
    evaluated: int
    stale: int
    future: int
    insufficient: int
    max_bar_age_seconds: float | None


def _return_at(closes: pd.Series, minutes: int) -> float | None:
    if len(closes) <= minutes:
        return None
    reference = float(closes.iloc[-(minutes + 1)])
    return float(closes.iloc[-1]) / reference - 1 if reference > 0 else None


def _tier(score: float) -> int:
    if score >= 2.0:
        return 3
    if score >= 1.5:
        return 2
    return 1


def _finite_decimal(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def evaluate_holding_price_alerts(
    bars: pd.DataFrame,
    positions: Sequence[Mapping[str, object]],
    now: datetime,
    settings: HoldingPriceAlertSettings,
    etf_symbols: frozenset[str],
) -> HoldingPriceAlertEvaluation:
    """从 1 分钟 OHLCV 中检测真实持仓异动，无 IO 副作用。"""
    if bars.empty or not positions:
        return HoldingPriceAlertEvaluation((), len(positions), 0, 0, 0, 0, 0, None)
    now_utc = now.astimezone(UTC)
    feed = str(bars.attrs.get("feed", "unknown"))
    output: list[Signal] = []
    available = set(bars.index.get_level_values("ticker"))
    available_count = 0
    evaluated = 0
    stale = 0
    future = 0
    insufficient = 0
    ages: list[float] = []

    for position in positions:
        ticker = str(position.get("symbol", "")).strip().upper()
        if not ticker or ticker not in available:
            continue
        available_count += 1
        frame = bars.xs(ticker, level="ticker").sort_index()
        if frame.empty:
            insufficient += 1
            continue
        timestamps = pd.DatetimeIndex(frame.index)
        if timestamps.tz is None:
            timestamps = timestamps.tz_localize("UTC")
        else:
            timestamps = timestamps.tz_convert("UTC")
        frame = frame.copy()
        frame.index = timestamps
        latest_at = timestamps[-1].to_pydatetime()
        age = (now_utc - latest_at).total_seconds()
        ages.append(age)
        if age < -15:
            future += 1
            continue
        if age > settings.max_bar_age_seconds:
            stale += 1
            continue

        local_dates = timestamps.tz_convert(_ET).date
        latest_date = local_dates[-1]
        session = frame.loc[local_dates == latest_date]
        closes = (
            session["close"].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        )
        if len(closes) < 2 or float(closes.iloc[-1]) <= 0:
            insufficient += 1
            continue
        evaluated += 1

        returns = closes.pct_change(fill_method=None).dropna()
        # 基线不包含当前触发根，否则异动本身会抬高门槛并稀释等级。
        baseline_returns = returns.iloc[:-1]
        volatility = (
            float(baseline_returns.tail(30).std(ddof=0))
            if len(baseline_returns) >= 5
            else 0.0
        )
        volatility = volatility if math.isfinite(volatility) else 0.0
        is_etf = ticker in etf_symbols
        prefix = "etf" if is_etf else "stock"
        base_thresholds = {
            "1分钟": float(getattr(settings, f"{prefix}_1m_pct")),
            "5分钟": float(getattr(settings, f"{prefix}_5m_pct")),
            "15分钟": float(getattr(settings, f"{prefix}_15m_pct")),
            "当日": float(getattr(settings, f"{prefix}_session_pct")),
        }
        moves = {
            "1分钟": _return_at(closes, 1),
            "5分钟": _return_at(closes, 5),
            "15分钟": _return_at(closes, 15),
            "当日": (
                float(closes.iloc[-1]) / float(session["open"].iloc[0]) - 1
                if float(session["open"].iloc[0]) > 0
                else None
            ),
        }
        elapsed = max(1, len(closes) - 1)
        effective_thresholds = {
            "1分钟": max(
                base_thresholds["1分钟"],
                volatility * settings.volatility_sigma_multiple_1m,
            ),
            "5分钟": max(
                base_thresholds["5分钟"],
                volatility * math.sqrt(5) * settings.volatility_sigma_multiple_5m,
            ),
            "15分钟": max(
                base_thresholds["15分钟"],
                volatility * math.sqrt(15) * settings.volatility_sigma_multiple_15m,
            ),
            "当日": max(
                base_thresholds["当日"],
                volatility
                * math.sqrt(elapsed)
                * settings.volatility_sigma_multiple_session,
            ),
        }

        candidates: list[tuple[float, str, float, float]] = []
        for window, move in moves.items():
            threshold = effective_thresholds[window]
            if move is not None and abs(move) >= threshold:
                candidates.append((abs(move) / threshold, window, move, threshold))

        volume_ratio: float | None = None
        volumes = session["volume"].astype(float).replace([np.inf, -np.inf], np.nan)
        if len(volumes) >= 6:
            typical_volume = float(volumes.iloc[:-1].tail(20).median())
            if typical_volume > 0:
                volume_ratio = float(volumes.iloc[-1]) / typical_volume
        one_minute_move = moves["1分钟"]
        if (
            volume_ratio is not None
            and one_minute_move is not None
            and volume_ratio >= settings.volume_spike_multiple
            and abs(one_minute_move) >= settings.min_volume_spike_move_pct
        ):
            candidates.append(
                (
                    max(
                        volume_ratio / settings.volume_spike_multiple,
                        abs(one_minute_move) / settings.min_volume_spike_move_pct,
                    ),
                    "放量1分钟",
                    one_minute_move,
                    settings.min_volume_spike_move_pct,
                )
            )
        if not candidates:
            continue

        score, window, move, threshold = max(candidates, key=lambda item: item[0])
        direction = Direction.BUY if move > 0 else Direction.SELL
        direction_key = "up" if move > 0 else "down"
        tier = _tier(score)
        price = float(closes.iloc[-1])
        avg_entry = _finite_decimal(position.get("avg_entry_price"))
        quantity = _finite_decimal(position.get("qty"))
        pnl_from_cost = price / avg_entry - 1 if avg_entry and avg_entry > 0 else None
        output.append(
            Signal(
                ticker=ticker,
                direction=direction,
                price=price,
                reason=(
                    f"{window}{'上涨' if move > 0 else '下跌'} {abs(move):.2%}，"
                    f"超过当前自适应门槛 {threshold:.2%}"
                ),
                strategy_id=STRATEGY_ID,
                ts=latest_at,
                extra={
                    "window": window,
                    "move_pct": move,
                    "threshold_pct": threshold,
                    "severity": tier,
                    "one_minute_pct": moves["1分钟"],
                    "five_minute_pct": moves["5分钟"],
                    "fifteen_minute_pct": moves["15分钟"],
                    "session_pct": moves["当日"],
                    "minute_volatility": volatility,
                    "volume_ratio": volume_ratio,
                    "quantity": quantity,
                    "avg_entry_price": avg_entry,
                    "pnl_from_cost_pct": pnl_from_cost,
                    "asset_type": "ETF" if is_etf else "个股",
                    "data_feed": feed,
                    "observed_at": latest_at.isoformat(),
                    "monitor_origin": str(
                        position.get("monitor_origin") or "holding"
                    ),
                },
                dedup_suffix=f"{window.replace('分钟', 'm').replace('当日', 'session').replace('放量1m', 'volume')}:{direction_key}:t{tier}",
            )
        )
    return HoldingPriceAlertEvaluation(
        signals=tuple(output),
        requested=len(positions),
        available=available_count,
        evaluated=evaluated,
        stale=stale,
        future=future,
        insufficient=insufficient,
        max_bar_age_seconds=max(ages) if ages else None,
    )


def detect_holding_price_alerts(
    bars: pd.DataFrame,
    positions: Sequence[Mapping[str, object]],
    now: datetime,
    settings: HoldingPriceAlertSettings,
    etf_symbols: frozenset[str],
) -> list[Signal]:
    """向后兼容的纯检测入口；生产流水线使用带诊断的 evaluate 入口。"""
    return list(
        evaluate_holding_price_alerts(
            bars, positions, now, settings, etf_symbols
        ).signals
    )
