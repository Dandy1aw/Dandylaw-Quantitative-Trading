from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from quant_signal.config import HoldingPriceAlertSettings
from quant_signal.holding_price_alert import (
    detect_holding_price_alerts,
    evaluate_holding_price_alerts,
)
from quant_signal.notifier.cards import holding_price_alert_card


def _bars(
    closes: list[float],
    *,
    ticker: str = "AAA",
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    ts = pd.date_range(
        datetime(2026, 8, 4, 14, 0, tzinfo=UTC),
        periods=len(closes),
        freq="1min",
    )
    volume = volumes or [10_000.0] * len(closes)
    prices = np.asarray(closes)
    with np.errstate(over="ignore"):
        highs = prices * 1.001
        lows = prices * 0.999
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volume,
        },
        index=pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"]),
    )


def _position(ticker: str = "AAA") -> dict[str, object]:
    return {
        "symbol": ticker,
        "qty": "5",
        "avg_entry_price": "95",
    }


def _threshold_bars(window: str, move: float, *, ticker: str) -> pd.DataFrame:
    target = round(100.0 * (1.0 + move), 6)
    if window == "1分钟":
        closes = [100.0] * 30 + [target]
    elif window == "5分钟":
        closes = [100.0] * 26 + [target] * 5
    elif window == "15分钟":
        closes = [100.0] * 16 + [target] * 15
    else:
        closes = [100.0] + [target] * 30
    return _bars(closes, ticker=ticker)


def _base_threshold_settings() -> HoldingPriceAlertSettings:
    return HoldingPriceAlertSettings(
        volatility_sigma_multiple_1m=0.001,
        volatility_sigma_multiple_5m=0.001,
        volatility_sigma_multiple_15m=0.001,
        volatility_sigma_multiple_session=0.001,
    )


@pytest.mark.parametrize(
    ("ticker", "etf_symbols", "window", "threshold"),
    [
        ("AAA", frozenset(), "1分钟", 0.020),
        ("AAA", frozenset(), "5分钟", 0.040),
        ("AAA", frozenset(), "15分钟", 0.060),
        ("AAA", frozenset(), "当日", 0.100),
        ("SPY", frozenset({"SPY"}), "1分钟", 0.012),
        ("SPY", frozenset({"SPY"}), "5分钟", 0.025),
        ("SPY", frozenset({"SPY"}), "15分钟", 0.040),
        ("SPY", frozenset({"SPY"}), "当日", 0.060),
    ],
)
def test_price_thresholds_do_not_trigger_below_boundary_and_trigger_at_boundary(
    ticker: str,
    etf_symbols: frozenset[str],
    window: str,
    threshold: float,
) -> None:
    now = datetime(2026, 8, 4, 14, 30, tzinfo=UTC)
    settings = _base_threshold_settings()

    below = detect_holding_price_alerts(
        _threshold_bars(window, threshold - 0.000001, ticker=ticker),
        [_position(ticker)],
        now,
        settings,
        etf_symbols,
    )
    at_boundary = detect_holding_price_alerts(
        _threshold_bars(window, threshold, ticker=ticker),
        [_position(ticker)],
        now,
        settings,
        etf_symbols,
    )

    assert below == []
    assert len(at_boundary) == 1
    signal = at_boundary[0]
    assert signal.direction.value == "buy"
    assert signal.extra is not None
    assert signal.extra["window"] == window
    assert signal.extra["move_pct"] == pytest.approx(threshold)
    assert signal.extra["threshold_pct"] == threshold
    assert signal.extra["strength_score"] == (
        abs(signal.extra["move_pct"]) / signal.extra["threshold_pct"]
    )
    assert signal.extra["severity"] == 1


def test_one_minute_stock_spike_triggers_and_card_is_observational() -> None:
    closes = [100.0] * 30 + [102.0]
    bars = _bars(closes)
    bars.attrs["feed"] = "alpaca_sip_1m"

    signals = detect_holding_price_alerts(
        bars,
        [_position()],
        now=datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        settings=HoldingPriceAlertSettings(),
        etf_symbols=frozenset(),
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.strategy_id == "holding_price_alert"
    assert signal.dedup_suffix == "1m:up:t1"
    assert signal.extra is not None
    assert signal.extra["window"] == "1分钟"
    card = holding_price_alert_card(signal)
    assert "持仓股价异动" in card.title
    assert "1分钟上涨" in card.body_md
    assert "相对成本" in card.body_md
    assert "alpaca_sip_1m" in card.body_md
    assert "建议买入" not in card.body_md
    assert "SELL 信号" not in card.body_md


def test_exact_boundary_triggers_for_non_round_reference_price() -> None:
    signals = detect_holding_price_alerts(
        _bars([1.23] * 30 + [1.2546]),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        _base_threshold_settings(),
        frozenset(),
    )

    assert len(signals) == 1
    assert signals[0].extra is not None
    assert signals[0].extra["window"] == "1分钟"
    assert signals[0].extra["move_pct"] == pytest.approx(0.020)
    assert signals[0].extra["threshold_pct"] == 0.020


def test_manual_symbol_signal_omits_position_context() -> None:
    position = {"symbol": "AAA", "monitor_origin": "manual"}
    signals = detect_holding_price_alerts(
        _bars([100.0] * 30 + [102.0]),
        [position],
        now=datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        settings=HoldingPriceAlertSettings(),
        etf_symbols=frozenset(),
    )

    card = holding_price_alert_card(signals[0])
    assert signals[0].extra["monitor_origin"] == "manual"
    assert "个股股价异动" in card.title
    assert "持仓语境" not in card.body_md


def test_adaptive_volatility_threshold_suppresses_a_noisy_stock() -> None:
    returns = [0.025 if index % 2 == 0 else -0.025 for index in range(30)]
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1 + value))
    closes[-1] = closes[-2] * 1.02

    signals = detect_holding_price_alerts(
        _bars(closes),
        [_position()],
        now=datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        settings=HoldingPriceAlertSettings(),
        etf_symbols=frozenset(),
    )

    assert signals == []


def test_etf_uses_lower_threshold_than_stock() -> None:
    closes = [100.0] * 30 + [101.2]
    now = datetime(2026, 8, 4, 14, 30, tzinfo=UTC)
    stock = detect_holding_price_alerts(
        _bars(closes), [_position()], now, HoldingPriceAlertSettings(), frozenset()
    )
    etf = detect_holding_price_alerts(
        _bars(closes, ticker="SMH"),
        [_position("SMH")],
        now,
        HoldingPriceAlertSettings(),
        frozenset({"SMH"}),
    )

    assert stock == []
    assert len(etf) == 1


def test_volume_spike_can_trigger_with_a_smaller_price_move() -> None:
    closes = [100.0] * 30 + [101.2]
    volumes = [10_000.0] * 30 + [60_000.0]

    signals = detect_holding_price_alerts(
        _bars(closes, volumes=volumes),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        HoldingPriceAlertSettings(),
        frozenset(),
    )

    assert len(signals) == 1
    assert signals[0].extra is not None
    assert signals[0].extra["window"] == "放量1分钟"
    assert signals[0].extra["threshold_pct"] == 0.010
    assert signals[0].extra["strength_score"] == (
        abs(signals[0].extra["move_pct"])
        / signals[0].extra["threshold_pct"]
    )
    assert signals[0].extra["severity"] == 1


def test_six_times_volume_does_not_increase_strength_or_severity() -> None:
    signals = detect_holding_price_alerts(
        _bars(
            [100.0] * 30 + [101.0],
            volumes=[10_000.0] * 30 + [60_000.0],
        ),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        HoldingPriceAlertSettings(),
        frozenset(),
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.extra is not None
    assert signal.extra["window"] == "放量1分钟"
    assert signal.extra["volume_ratio"] == 6.0
    assert signal.extra["strength_score"] == pytest.approx(1.0)
    assert signal.extra["severity"] == 1


@pytest.mark.parametrize(
    ("move", "volume_multiple"),
    [(0.009999, 4.0), (0.010, 3.999)],
)
def test_volume_spike_requires_price_and_volume_boundaries(
    move: float, volume_multiple: float
) -> None:
    closes = [100.0] * 30 + [100.0 * (1.0 + move)]
    volumes = [10_000.0] * 30 + [10_000.0 * volume_multiple]

    signals = detect_holding_price_alerts(
        _bars(closes, volumes=volumes),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        _base_threshold_settings(),
        frozenset(),
    )

    assert signals == []


def test_volume_spike_triggers_at_exact_price_and_volume_boundaries() -> None:
    closes = [100.0] * 30 + [101.0]
    volumes = [10_000.0] * 30 + [40_000.0]

    signals = detect_holding_price_alerts(
        _bars(closes, volumes=volumes),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        _base_threshold_settings(),
        frozenset(),
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction.value == "buy"
    assert signal.extra is not None
    assert signal.extra["window"] == "放量1分钟"
    assert signal.extra["move_pct"] == pytest.approx(0.010)
    assert signal.extra["threshold_pct"] == 0.010
    assert signal.extra["strength_score"] == (
        abs(signal.extra["move_pct"])
        / signal.extra["threshold_pct"]
    )
    assert signal.extra["severity"] == 1


def test_nonfinite_move_from_finite_extreme_closes_fails_closed() -> None:
    signals = detect_holding_price_alerts(
        _bars([1e-308] * 30 + [1e308]),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        _base_threshold_settings(),
        frozenset(),
    )

    assert signals == []


def test_stale_bars_do_not_alert() -> None:
    bars = _bars([100.0] * 30 + [103.0])
    signals = detect_holding_price_alerts(
        bars,
        [_position()],
        datetime(2026, 8, 4, 15, 0, tzinfo=UTC),
        HoldingPriceAlertSettings(max_bar_age_seconds=120),
        frozenset(),
    )
    assert signals == []

    evaluation = evaluate_holding_price_alerts(
        bars,
        [_position()],
        datetime(2026, 8, 4, 15, 0, tzinfo=UTC),
        HoldingPriceAlertSettings(max_bar_age_seconds=120),
        frozenset(),
    )
    assert evaluation.requested == 1
    assert evaluation.available == 1
    assert evaluation.evaluated == 0
    assert evaluation.stale == 1
    assert evaluation.max_bar_age_seconds == 1800


def test_card_includes_codex_cause_confidence_and_clickable_sources() -> None:
    signal = detect_holding_price_alerts(
        _bars([100.0] * 30 + [102.0]),
        [_position()],
        datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        HoldingPriceAlertSettings(),
        frozenset(),
    )[0]
    extra = dict(signal.extra or {})
    extra["price_move_cause"] = {
        "category": "company_news",
        "confidence": "high",
        "summary": "公司发布重大管理层调整，与异动时间接近。",
        "search_status": "ok",
        "elapsed_seconds": 8.2,
        "evidence": [
            {
                "title": "Company announces management change",
                "source": "Reuters",
                "url": "https://www.reuters.com/example",
                "published_at": "2026-08-04T14:25:00Z",
            }
        ],
    }

    card = holding_price_alert_card(replace(signal, extra=extra))

    assert "Codex实时查因" in card.body_md
    assert "公司消息 · 置信度高" in card.body_md
    assert "[来源1](https://www.reuters.com/example)" in card.body_md
    assert "相关性不等于因果" in card.body_md
