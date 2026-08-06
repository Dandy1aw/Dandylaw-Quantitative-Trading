from datetime import date

import numpy as np
import pandas as pd

from quant_signal.candidate_lanes import (
    CandidateLane,
    _buying_pressure,
    _candidate,
    discover_candidates,
)
from quant_signal.config import CandidateLaneSettings
from quant_signal.market_regime import Regime


DAY = date(2026, 7, 14)
CFG = CandidateLaneSettings(
    top_n_per_lane=3,
    minimum_price=5,
    min_dollar_volume=1_000_000,
)


def _bars(paths: dict[str, np.ndarray], volumes: dict[str, float] | None = None) -> pd.DataFrame:
    ts = pd.date_range(end=DAY, periods=220, freq="B", tz="UTC")
    frames: list[pd.DataFrame] = []
    for ticker, path in paths.items():
        close = np.asarray(path, dtype=float)
        volume = (volumes or {}).get(ticker, 2_000_000)
        frames.append(
            pd.DataFrame(
                {
                    "open": close * 0.998,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": np.full(len(close), volume),
                },
                index=pd.MultiIndex.from_product(
                    [[ticker], ts[-len(close) :]], names=["ticker", "ts"]
                ),
            )
        )
    return pd.concat(frames).sort_index()


def _trend_paths(count: int = 5) -> dict[str, np.ndarray]:
    paths: dict[str, np.ndarray] = {}
    for index in range(count):
        slope = 0.20 + index * 0.015
        paths[f"T{index}"] = 100 * np.exp(np.linspace(0, slope, 220))
    return paths


def test_holdings_do_not_change_candidate_output() -> None:
    paths = _trend_paths()
    members = set(paths)

    first = discover_candidates(
        _bars(paths), members, Regime.TREND, as_of=DAY, settings=CFG, holdings={"MU"}
    )
    second = discover_candidates(
        _bars(paths), members, Regime.TREND, as_of=DAY, settings=CFG, holdings={"AAPL"}
    )

    assert first == second
    assert first.candidates
    assert all(row.lane == CandidateLane.TREND_CONTINUATION for row in first.candidates)


def test_new_ads_is_observation_until_history_is_ready() -> None:
    paths = _trend_paths(1)
    paths["SKHY"] = np.linspace(40, 44, 10)

    result = discover_candidates(
        _bars(paths), set(paths), Regime.TREND, as_of=DAY, settings=CFG
    )

    assert all(row.ticker != "SKHY" for row in result.candidates)
    observation = next(row for row in result.observations if row.ticker == "SKHY")
    assert observation.reason == "INSUFFICIENT_HISTORY"
    assert observation.history_days == 10


def test_risk_off_blocks_all_long_lanes() -> None:
    paths = _trend_paths()
    result = discover_candidates(
        _bars(paths), set(paths), Regime.RISK_OFF, as_of=DAY, settings=CFG
    )

    assert result.candidates == ()
    assert result.block_reason == "REGIME_RISK_OFF"


def test_trend_pullback_is_separate_from_continuation() -> None:
    path = 100 * np.exp(np.linspace(0, 0.30, 220))
    path[-10:] = np.linspace(path[-11] * 1.005, path[-11] * 0.965, 10)

    result = discover_candidates(
        _bars({"PULL": path}), {"PULL"}, Regime.PULLBACK, as_of=DAY, settings=CFG
    )

    row = next(candidate for candidate in result.candidates if candidate.ticker == "PULL")
    assert row.lane == CandidateLane.TREND_PULLBACK
    assert row.invalidation_price < row.entry_low <= row.entry_high < row.target_price


def test_range_reversion_requires_reversal_confirmation() -> None:
    x = np.linspace(0, 10 * np.pi, 220)
    path = 100 + np.sin(x) * 4
    path[-8:] = [99, 97, 95, 94, 93, 92, 92.5, 93.5]

    result = discover_candidates(
        _bars({"REV": path}), {"REV"}, Regime.RANGE, as_of=DAY, settings=CFG
    )

    row = next(candidate for candidate in result.candidates if candidate.ticker == "REV")
    assert row.lane == CandidateLane.RANGE_REVERSION
    assert "REVERSAL_CONFIRMED" in row.reasons


def test_overheated_ticker_is_avoid_observation() -> None:
    path = np.linspace(90.0, 110.0, 220)
    path[-3:] = [112.0, 125.0, 150.0]

    result = discover_candidates(
        _bars({"HOT": path}), {"HOT"}, Regime.TREND, as_of=DAY, settings=CFG
    )

    assert result.candidates == ()
    observation = next(row for row in result.observations if row.ticker == "HOT")
    assert observation.reason == "OVERHEATED"


def test_lane_cap_and_cross_lane_deduplication() -> None:
    paths = _trend_paths(6)

    result = discover_candidates(
        _bars(paths), set(paths), Regime.TREND, as_of=DAY, settings=CFG
    )

    assert len(result.candidates) == 3
    assert len({row.ticker for row in result.candidates}) == 3


def test_cross_lane_cluster_cap_prevents_hidden_theme_concentration() -> None:
    paths = _trend_paths(6)
    settings = CFG.model_copy(update={"max_candidates_per_cluster": 2})

    result = discover_candidates(
        _bars(paths),
        set(paths),
        Regime.TREND,
        as_of=DAY,
        settings=settings,
        risk_clusters={"single_theme": list(paths)},
    )

    assert len(result.candidates) == 2
    assert all(row.ticker in paths for row in result.candidates)


def test_earnings_veto_keeps_reason_without_trade_levels() -> None:
    paths = _trend_paths(1)
    result = discover_candidates(
        _bars(paths), set(paths), Regime.TREND, as_of=DAY, settings=CFG,
        earnings_blocked={"T0"},
    )

    assert result.candidates == ()
    assert result.observations[0].reason == "EARNINGS_WINDOW"


def test_candidate_uses_three_fixed_five_percent_profit_targets() -> None:
    paths = _trend_paths(1)

    result = discover_candidates(
        _bars(paths), set(paths), Regime.TREND, as_of=DAY, settings=CFG
    )

    row = result.candidates[0]
    assert row.profit_targets == (
        round(row.entry_high * 1.05, 4),
        round(row.entry_high * 1.10, 4),
        round(row.entry_high * 1.15, 4),
    )
    assert row.target_price == row.profit_targets[row.recommended_target_stage - 1]


def test_sustained_rise_with_strong_buying_pressure_recommends_third_target() -> None:
    paths = _trend_paths(1)

    result = discover_candidates(
        _bars(paths), set(paths), Regime.TREND, as_of=DAY, settings=CFG
    )

    row = result.candidates[0]
    assert row.recent_buying_notional > 0
    assert row.buying_pressure_score >= 0.65
    assert row.buying_pressure_label == "强"
    assert row.recommended_target_stage == 3
    assert row.target_price == row.profit_targets[2]


def test_mixed_recent_buying_pressure_recommends_second_target() -> None:
    close = np.asarray([100.0] * 15 + [101.0, 100.0, 101.0, 100.0, 101.0])
    frame = pd.DataFrame(
        {
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1_000_000),
        }
    )

    buying_notional, score, label, stage = _buying_pressure(frame)

    assert buying_notional > 0
    assert 0.40 <= score < 0.65
    assert label == "中等"
    assert stage == 2


def test_nearby_resistance_caps_strong_buying_pressure_target_stage() -> None:
    close = np.concatenate(
        (
            np.linspace(80.0, 115.0, 141),
            np.linspace(110.0, 100.0, 74),
            np.asarray([101.0, 102.0, 103.0, 104.0, 105.0]),
        )
    )
    frame = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1_000_000),
        }
    )

    row = _candidate(
        "RESIST",
        frame,
        CandidateLane.TREND_PULLBACK,
        1.0,
        ("TEST",),
        CFG,
    )

    assert row is not None
    assert row.buying_pressure_label == "强"
    assert row.nearby_resistance is not None
    assert row.profit_targets[0] < row.nearby_resistance < row.profit_targets[1]
    assert row.recommended_target_stage == 1
    assert row.target_price == row.profit_targets[0]
