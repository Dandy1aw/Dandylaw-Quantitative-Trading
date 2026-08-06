from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from quant_signal.config import CandidateLaneSettings, MarketRegimeSettings
from quant_signal.candidate_lanes import Candidate, CandidateLane
from quant_signal.point_in_time import CertifiedMembershipMap, MembershipSnapshot

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "backtest_us_candidate_lanes.py"
_SPEC = importlib.util.spec_from_file_location("backtest_us_candidate_lanes", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
replay_candidate_lanes = _MODULE.replay_candidate_lanes
render_markdown = _MODULE.render_markdown
replay_candidate_comparison = getattr(_MODULE, "replay_candidate_comparison", None)
render_comparison_markdown = getattr(_MODULE, "render_comparison_markdown", None)
entry_fill_price = getattr(_MODULE, "entry_fill_price", None)
exit_fill_price = getattr(_MODULE, "exit_fill_price", None)
entry_session_exit_price = getattr(_MODULE, "entry_session_exit_price", None)
load_point_in_time_members = getattr(_MODULE, "load_point_in_time_members", None)
require_release_eligible = getattr(_MODULE, "require_release_eligible", None)


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
        "research_release_eligible",
        "average_mfe",
        "average_mae",
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
    effective = datetime.combine(sessions[0], datetime.min.time(), tzinfo=timezone.utc)
    history = CertifiedMembershipMap(
        source="licensed-test-feed",
        source_version="fixture-v1",
        snapshots=(
            MembershipSnapshot(
                effective_at=effective,
                known_at=effective - timedelta(days=1),
                members=frozenset(members),
            ),
        ),
    )

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
    assert result["research_release_eligible"] is True


def test_research_release_gate_fails_closed_without_certified_membership() -> None:
    assert require_release_eligible is not None
    with pytest.raises(ValueError, match="point-in-time"):
        require_release_eligible({"research_release_eligible": False})
    require_release_eligible({"research_release_eligible": True})


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


def test_comparison_reports_sector_quota_and_lookahead_sensitivity() -> None:
    assert replay_candidate_comparison is not None
    bars, members = _bars()
    sector_by_ticker = {
        ticker: (
            "Information Technology"
            if index < 6
            else "Consumer Discretionary"
        )
        for index, ticker in enumerate(sorted(members))
    }
    comparison = replay_candidate_comparison(
        bars,
        members,
        regime_settings=MarketRegimeSettings(
            min_history_days=201, high_volatility_annualized=0.5
        ),
        lane_settings=CandidateLaneSettings(min_dollar_volume=1_000_000),
        sector_by_ticker=sector_by_ticker,
        current_large_cap_tickers=set(sorted(members)[2:]),
    )

    assert set(comparison) == {
        "baseline",
        "sector_quota",
        "current_large_cap_sensitivity",
    }
    assert comparison["current_large_cap_sensitivity"]["CURRENT_PROFILE_LOOKAHEAD"] is True
    assert comparison["current_large_cap_sensitivity"]["research_release_eligible"] is False
    assert comparison["sector_quota"]["max_sector_signal_share"] <= 1
    assert comparison["sector_quota"]["semiconductor_signal_share"] <= 1


def test_comparison_markdown_discloses_current_profile_lookahead() -> None:
    assert render_comparison_markdown is not None
    metrics = {
        "total_return": 0.1,
        "annualized_return": 0.1,
        "max_drawdown": -0.1,
        "sharpe": 1.0,
        "annual_turnover": 1.0,
        "trade_count": 2,
        "max_sector_signal_share": 0.5,
        "semiconductor_signal_share": 0.25,
    }
    report = render_comparison_markdown(
        {
            "baseline": dict(metrics),
            "sector_quota": dict(metrics),
            "current_large_cap_sensitivity": {
                **metrics,
                "CURRENT_PROFILE_LOOKAHEAD": True,
            },
        }
    )

    assert "baseline" in report and "sector-quota" in report
    assert "CURRENT_PROFILE_LOOKAHEAD=true" in report
    assert "不能作为上线收益证明" in report


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


def test_pre_entry_gap_target_is_not_counted_as_post_entry_profit() -> None:
    assert entry_session_exit_price is not None
    bar = pd.Series({"open": 120.0, "high": 121.0, "low": 103.0, "close": 104.0})

    assert entry_fill_price(bar, _candidate()) == 105.0
    assert entry_session_exit_price(
        bar, _candidate(), time_exit=False
    ) is None


def test_point_in_time_membership_json_loader(tmp_path: Path) -> None:
    assert load_point_in_time_members is not None
    path = tmp_path / "membership.json"
    path.write_text(
        '{"schema_version":"pit-membership-v1",'
        '"source":"licensed-test-feed","source_version":"fixture-v1",'
        '"snapshots":['
        '{"effective_at":"2026-07-14T00:00:00Z",'
        '"known_at":"2026-07-13T20:00:00Z","members":["aapl","msft"]},'
        '{"effective_at":"2026-07-15T00:00:00Z",'
        '"known_at":"2026-07-14T20:00:00Z","members":["nvda"]}'
        ']}',
        encoding="utf-8",
    )

    loaded = load_point_in_time_members(path)

    assert loaded.source == "licensed-test-feed"
    assert loaded.source_version == "fixture-v1"
    assert loaded[date(2026, 7, 14)] == {"AAPL", "MSFT"}
    assert loaded[date(2026, 7, 15)] == {"NVDA"}


def test_uncertified_membership_map_does_not_clear_bias_flag() -> None:
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

    assert result["survivorship_bias"] is True
    assert result["research_release_eligible"] is False
    assert result["average_mfe"] >= 0
    assert result["average_mae"] <= 0


def test_point_in_time_loader_rejects_legacy_or_lookahead_data(tmp_path: Path) -> None:
    assert load_point_in_time_members is not None
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"2026-07-14":["AAPL"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_point_in_time_members(legacy)

    lookahead = tmp_path / "lookahead.json"
    lookahead.write_text(
        '{"schema_version":"pit-membership-v1","source":"feed",'
        '"source_version":"v1","snapshots":[{'
        '"effective_at":"2026-07-14T00:00:00Z",'
        '"known_at":"2026-07-15T00:00:00Z","members":["AAPL"]}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="known_at"):
        load_point_in_time_members(lookahead)
