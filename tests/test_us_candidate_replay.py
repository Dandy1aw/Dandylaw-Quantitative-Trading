from datetime import date
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from quant_signal.config import CandidateLaneSettings, MarketRegimeSettings
from quant_signal.candidate_lanes import Candidate, CandidateLane

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "backtest_us_candidate_lanes.py"
_SPEC = importlib.util.spec_from_file_location("backtest_us_candidate_lanes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
replay_candidate_lanes = _MODULE.replay_candidate_lanes
render_markdown = _MODULE.render_markdown
entry_fill_price = getattr(_MODULE, "entry_fill_price", None)
exit_fill_price = getattr(_MODULE, "exit_fill_price", None)
load_point_in_time_members = getattr(_MODULE, "load_point_in_time_members", None)


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
    assert result["same_close_entries"] == 0
    assert result["min_entry_delay_sessions"] >= 1
    assert result["fill_rate"] <= 1


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
            "same_close_entries": 0,
            "min_entry_delay_sessions": 1,
            "fill_rate": 0.5,
            "stop_exits": 2,
            "target_exits": 3,
            "time_exits": 1,
        }
    )

    assert "幸存者偏差：是" in report
    assert "下一交易日执行" in report
    assert "5 bps" in report
    assert "同收盘成交：0" in report


def _candidate() -> Candidate:
    return Candidate(
        ticker="AAA",
        lane=CandidateLane.TREND_PULLBACK,
        score=1.0,
        price=104.0,
        entry_low=100.0,
        entry_high=105.0,
        invalidation_price=95.0,
        target_price=115.0,
        atr=3.0,
        history_days=220,
        reasons=("TEST",),
    )


def test_next_session_does_not_fill_above_entry_zone() -> None:
    assert entry_fill_price is not None
    bar = pd.Series({"open": 110.0, "high": 112.0, "low": 109.0, "close": 111.0})

    assert entry_fill_price(bar, _candidate()) is None


def test_entry_zone_cross_uses_deterministic_limit_fill() -> None:
    assert entry_fill_price is not None
    bar = pd.Series({"open": 110.0, "high": 111.0, "low": 103.0, "close": 104.0})

    assert entry_fill_price(bar, _candidate()) == 105.0


def test_same_bar_stop_and_target_is_conservatively_stopped() -> None:
    assert exit_fill_price is not None
    bar = pd.Series({"open": 104.0, "high": 118.0, "low": 92.0, "close": 110.0})

    assert exit_fill_price(
        bar, stop=95.0, target=115.0, time_exit=False
    ) == (95.0, "STOP")


def test_point_in_time_membership_json_loader(tmp_path: Path) -> None:
    assert load_point_in_time_members is not None
    path = tmp_path / "membership.json"
    path.write_text(
        '{"2026-07-14": ["aapl", "msft"], "2026-07-15": ["nvda"]}',
        encoding="utf-8",
    )

    loaded = load_point_in_time_members(path)

    assert loaded == {
        date(2026, 7, 14): {"AAPL", "MSFT"},
        date(2026, 7, 15): {"NVDA"},
    }
