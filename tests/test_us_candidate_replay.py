from datetime import date
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from quant_signal.config import CandidateLaneSettings, MarketRegimeSettings

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "backtest_us_candidate_lanes.py"
_SPEC = importlib.util.spec_from_file_location("backtest_us_candidate_lanes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
replay_candidate_lanes = _MODULE.replay_candidate_lanes
render_markdown = _MODULE.render_markdown


def _bars() -> tuple[pd.DataFrame, set[str]]:
    ts = pd.bdate_range("2025-06-02", periods=280, tz="UTC")
    members = {f"N{i:02d}" for i in range(10)}
    frames: list[pd.DataFrame] = []
    paths = {
        ticker: 100 * np.exp(np.linspace(0, 0.20 + index * 0.02, len(ts)))
        for index, ticker in enumerate(sorted(members))
    }
    paths["QQQ"] = 400 * np.exp(np.linspace(0, 0.20, len(ts)))
    for ticker, close in paths.items():
        frames.append(
            pd.DataFrame(
                {
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": np.full(len(close), 2_000_000),
                },
                index=pd.MultiIndex.from_product(
                    [[ticker], ts], names=["ticker", "ts"]
                ),
            )
        )
    return pd.concat(frames).sort_index(), members


def test_replay_reports_risk_return_turnover_and_bias() -> None:
    bars, members = _bars()
    result = replay_candidate_lanes(
        bars,
        members,
        regime_settings=MarketRegimeSettings(
            min_history_days=201, high_volatility_annualized=0.5
        ),
        lane_settings=CandidateLaneSettings(min_dollar_volume=1_000_000),
        transaction_cost_bps=5,
    )

    assert set(result) >= {
        "total_return",
        "annualized_return",
        "max_drawdown",
        "sharpe",
        "annual_turnover",
        "win_rate",
        "trade_count",
        "qqq_return",
        "relative_return",
        "survivorship_bias",
    }
    assert result["trade_count"] > 0
    assert result["annual_turnover"] >= 0
    assert result["survivorship_bias"] is True


def test_point_in_time_membership_clears_bias_flag() -> None:
    bars, members = _bars()
    sessions = sorted(set(bars.index.get_level_values("ts").date))
    history = {day: set(members) for day in sessions}

    result = replay_candidate_lanes(
        bars,
        members,
        regime_settings=MarketRegimeSettings(
            min_history_days=201, high_volatility_annualized=0.5
        ),
        lane_settings=CandidateLaneSettings(min_dollar_volume=1_000_000),
        point_in_time_members=history,
    )

    assert result["survivorship_bias"] is False


def test_markdown_makes_bias_and_execution_delay_explicit() -> None:
    report = render_markdown(
        {
            "total_return": 0.1,
            "annualized_return": 0.12,
            "max_drawdown": -0.08,
            "sharpe": 1.1,
            "annual_turnover": 4.2,
            "win_rate": 0.53,
            "trade_count": 40,
            "qqq_return": 0.08,
            "relative_return": 0.02,
            "survivorship_bias": True,
            "transaction_cost_bps": 5,
        }
    )

    assert "幸存者偏差：是" in report
    assert "下一交易日执行" in report
    assert "5 bps" in report
