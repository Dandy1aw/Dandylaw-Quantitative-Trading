from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from quant_signal.config import (
    DataQASettings,
    EnrichmentSettings,
    ExecutionPlanSettings,
    ForwardEvaluationSettings,
    IndexUniverseSettings,
    NotifySettings,
    OptionFlowSettings,
    Settings,
    USBriefingSettings,
    load_settings,
)

import pytest


def test_load_settings_from_repo_yaml() -> None:
    s = load_settings()
    assert s.data_source in ("yfinance", "alpaca")  # 用户在 settings.yaml 中可切换
    assert "SPY" in s.universe
    assert s.ai_briefing.enabled is True
    assert s.ai_briefing.provider == "codex_cli"
    assert s.ai_briefing.command == "codex"
    assert s.strategies["momentum_rotation"]["top_n"] == 3
    assert s.notify.dedup_hours == 4
    assert s.notify.action_card_only is False
    # 趋势闸门：阶段B回测选出的最优配置(仅200线+防御，关绝对动量)
    assert s.trend_gate.enabled is True
    assert s.trend_gate.use_mom is False
    assert s.trend_gate.defensive == ["BIL", "TLT", "GLD"]
    assert s.index_universe.enabled is True
    assert s.index_universe.indices == ["sp500", "nasdaq100"]
    assert s.execution_plan.enabled is True
    assert s.execution_plan.account_provider == "screenshot"
    assert s.execution_plan.capital_limit_usd == 6000
    assert s.execution_plan.max_financing_ratio == 0.20
    assert s.execution_plan.cash_reserve == 0
    assert s.legacy_price_deviation.enabled is False
    assert s.option_flow.enabled is True
    assert s.option_flow.feed == "indicative"
    assert s.option_flow.venues == ["cone", "ctwo", "opt", "exo"]
    assert s.option_flow.top_n == 10
    assert s.option_flow.max_alerts_per_day == 4
    assert s.option_flow.retention_days == 120
    assert s.holding_price_alert.enabled is True
    assert s.holding_price_alert.stock_1m_pct == 0.015
    assert s.holding_price_alert.etf_1m_pct == 0.010
    assert s.holding_price_alert.cooldown_minutes == 30
    assert s.holding_price_alert.cause_search.enabled is True
    assert s.holding_price_alert.cause_search.command == "codex"
    assert s.holding_price_alert.cause_search.model == "gpt-5.6-terra"
    assert s.holding_price_alert.cause_search.reasoning_effort == "low"
    assert s.holding_price_alert.cause_search.timeout_seconds == 60
    assert set(s.universe) == set(s.tickers)
    assert set(s.watchlist) == {"NVDA", "TSLA", "AAPL", "MSFT", "AMD"}


def test_us_briefing_defaults_are_safe() -> None:
    settings = USBriefingSettings()
    assert settings.enabled is False
    assert settings.delivery_mode == "shadow"
    assert settings.candidate_index == "nasdaq100"
    assert settings.min_coverage == 0.98
    assert settings.morning_hour_utc == 0
    assert settings.afternoon_hour_utc == 7
    assert settings.afternoon_minute_utc == 30
    assert settings.position_discipline.allow_financing_for_leveraged is False


def test_data_qa_defaults_match_production_decision_coverage() -> None:
    settings = DataQASettings()
    assert settings.min_coverage == 0.98
    assert settings.divergence_threshold == 0.005
    assert settings.batch_size == 50
    assert settings.lookback_days == 7


def test_forward_evaluation_defaults_capture_multiple_horizons_and_costs() -> None:
    settings = ForwardEvaluationSettings(horizons=(21, 5, 21), benchmark=" qqq ")
    assert settings.horizons == (5, 21)
    assert settings.benchmark == "QQQ"
    assert settings.transaction_cost_bps_per_side == 5.0


def test_repo_universe_uses_skhynix_ads_not_korean_listing() -> None:
    settings = load_settings()
    assert "000660.KS" not in settings.tickers
    assert "SKHY" in settings.tickers
    assert settings.tickers["SKHY"].currency == "USD"
    assert "SKHY" in settings.execution_plan.risk_clusters["semiconductor_memory"]
    assert settings.option_intel.wall_high_min_oi_each_side == 5_000
    assert settings.option_intel.wall_medium_min_oi_each_side == 1_000
    assert settings.option_intel.gamma_near_spot_pct == 0.02
    assert "KRW" not in settings.momentum_group_top_n
    assert settings.us_briefing.enabled is True
    assert settings.us_briefing.delivery_mode == "live"
    assert settings.us_briefing.candidate_lanes.earnings_blackout_days == 2
    assert settings.us_briefing.candidate_lanes.max_candidates_per_cluster == 2
    assert set(settings.execution_plan.risk_clusters) >= {
        "semiconductor_memory",
        "mega_cap_platforms",
        "enterprise_software",
        "consumer_growth",
        "biotech_healthcare",
    }


def test_env_credentials_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式置空而非 delenv：load_dotenv 默认不覆盖已存在的环境变量，这样测试
    # 不受本机 config/.env 是否真的填了 webhook 影响，保持隔离。
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    s = load_settings()
    assert s.feishu_webhook == ""


@pytest.mark.parametrize("ticker", ["AAA", "7709.HK"])
def test_settings_rejects_unclassified_universe_ticker(ticker: str) -> None:
    with pytest.raises(ValidationError, match=ticker):
        Settings(
            universe=[ticker],
            watchlist=[],
            strategies={"momentum_rotation": {}, "breakout_20d": {}},
            asset_type={},
            international_tickers={},
        )


def test_settings_accepts_foreign_ticker_with_currency_mapping() -> None:
    settings = Settings(
        universe=["7709.HK"],
        watchlist=[],
        strategies={"momentum_rotation": {}, "breakout_20d": {}},
        asset_type={},
        international_tickers={"7709.HK": "HKD"},
    )
    assert settings.international_tickers == {"7709.HK": "HKD"}


def test_enrichment_timeout_defaults_to_sixty_seconds() -> None:
    assert EnrichmentSettings().timeout_seconds == 60


def test_ticker_registry_derives_legacy_market_fields() -> None:
    settings = Settings(
        tickers={
            "SPY": {"asset_type": "ETF", "currency": "USD"},
            "7709.HK": {"asset_type": "STOCK", "currency": "HKD"},
        },
        watchlist=["SPY"],
        strategies={"momentum_rotation": {}, "breakout_20d": {}},
    )

    assert settings.universe == ["SPY", "7709.HK"]
    assert settings.asset_type == {"SPY": "ETF", "7709.HK": "STOCK"}
    assert settings.international_tickers == {"7709.HK": "HKD"}


def test_ticker_registry_derives_leverage_factor() -> None:
    settings = Settings(
        tickers={
            "SPY": {"asset_type": "ETF", "currency": "USD"},
            "SNXX": {"asset_type": "ETF", "currency": "USD", "leverage": 2},
        },
        strategies={"momentum_rotation": {}, "breakout_20d": {}},
    )
    assert settings.leverage_factor == {"SNXX": 2.0}   # 1x 不入 map


def test_ticker_registry_rejects_sub_one_leverage() -> None:
    with pytest.raises(ValueError, match="leverage"):
        Settings(
            tickers={"BAD": {"asset_type": "ETF", "currency": "USD", "leverage": 0.5}},
            strategies={"momentum_rotation": {}, "breakout_20d": {}},
        )


def test_legacy_hourly_limit_populates_all_notification_channels() -> None:
    settings = NotifySettings(hourly_limit=3)
    assert settings.premarket_hourly_limit == 3
    assert settings.intraday_hourly_limit == 3
    assert settings.deviation_hourly_limit == 3


def test_index_universe_does_not_expand_core_strategy_universe() -> None:
    settings = Settings(
        universe=["AAA", "BBB"],
        watchlist=["AAA"],
        strategies={"momentum_rotation": {}, "breakout_20d": {}},
        asset_type={"AAA": "STOCK", "BBB": "STOCK"},
        index_universe={
            "enabled": True,
            "indices": ["sp500", "nasdaq100"],
        },
    )

    assert settings.universe == ["AAA", "BBB"]
    assert settings.watchlist == ["AAA"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("refresh_days", 0),
        ("max_stale_days", 61),
        ("scan_top_n", 4),
        ("execution_top_n", 11),
        ("min_coverage", 0.79),
        ("min_dollar_volume", 0),
    ],
)
def test_index_universe_rejects_out_of_bounds_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        IndexUniverseSettings(**{field: value})


def test_index_universe_requires_execution_pool_within_scan_pool() -> None:
    with pytest.raises(ValidationError, match="execution_top_n"):
        IndexUniverseSettings(scan_top_n=5, execution_top_n=6)


def test_index_universe_requires_stale_window_to_cover_refresh_window() -> None:
    with pytest.raises(ValidationError, match="max_stale_days"):
        IndexUniverseSettings(refresh_days=15, max_stale_days=14)


@pytest.mark.parametrize("indices", [[], ["sp500", "sp500"]])
def test_index_universe_requires_nonempty_unique_indices(indices: list[str]) -> None:
    with pytest.raises(ValidationError, match="indices"):
        IndexUniverseSettings(indices=indices)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade", 0),
        ("risk_per_trade", 0.051),
        ("max_daily_new_risk", 0.101),
        ("max_position_weight", 0.51),
        ("max_cluster_weight", 1.01),
        ("cash_reserve", 1.0),
        ("max_new_positions_per_day", 0),
        ("quote_max_age_seconds", 59),
        ("account_max_age_seconds", 601),
        ("capital_limit_usd", 0),
        ("max_financing_ratio", 0.21),
        ("screenshot_max_age_hours", 0),
    ],
)
def test_execution_plan_rejects_invalid_risk_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        ExecutionPlanSettings(**{field: value})


def test_execution_plan_requires_stop_distance_order() -> None:
    with pytest.raises(ValidationError, match="max_stop_distance"):
        ExecutionPlanSettings(min_stop_distance=0.20, max_stop_distance=0.20)


def test_execution_plan_risk_clusters_are_unique_and_normalized() -> None:
    settings = ExecutionPlanSettings(
        risk_clusters={"semiconductor_memory": ["MU", "SMH", "SNXX"]}
    )
    assert settings.risk_clusters["semiconductor_memory"] == ["MU", "SMH", "SNXX"]

    with pytest.raises(ValidationError, match="risk_clusters"):
        ExecutionPlanSettings(risk_clusters={"a": ["MU"], "b": ["MU"]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_n", 0),
        ("discovery_limit", 9),
        ("min_volume", -1),
        ("rank_jump", 0),
        ("cooldown_minutes", 14),
        ("max_alerts_per_day", 1),
        ("intraday_expiry_minutes", 14),
        ("min_venue_coverage", 0.49),
        ("retention_days", 29),
    ],
)
def test_option_flow_rejects_unsafe_policy(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        OptionFlowSettings(**{field: value})


def test_option_flow_retention_defaults_to_120_days() -> None:
    assert OptionFlowSettings().retention_days == 120


def test_option_flow_holding_monitor_defaults_and_limits() -> None:
    settings = OptionFlowSettings()
    assert settings.holding_monitor_enabled is True
    assert settings.holding_max_expiry_days == 14
    assert settings.holding_max_tickers == 12
    assert settings.holding_min_delta_volume == 5_000
    assert settings.holding_dominance_threshold == pytest.approx(0.20)

    for field, value in (
        ("holding_max_expiry_days", 0),
        ("holding_max_tickers", 0),
        ("holding_min_delta_volume", -1),
        ("holding_dominance_threshold", 1.01),
    ):
        with pytest.raises(ValidationError, match=field):
            OptionFlowSettings(**{field: value})


def test_option_flow_requires_complete_unique_venues_when_enabled() -> None:
    with pytest.raises(ValidationError, match="venues"):
        OptionFlowSettings(enabled=True, venues=["cone", "ctwo", "opt"])
    with pytest.raises(ValidationError, match="venues"):
        OptionFlowSettings(enabled=True, venues=["cone", "cone", "opt", "exo"])


def test_feishu_bot_settings_defaults_and_normalization() -> None:
    from quant_signal.config import FeishuBotSettings

    settings = FeishuBotSettings()
    assert settings.enabled is False
    assert settings.allowed_open_ids == []
    assert settings.capital_limit == Decimal("6000")
    assert settings.max_financing_ratio == Decimal("0.20")
    assert settings.confirm_window_minutes == 15
    assert settings.codex_timeout_seconds == 180

    tuned = FeishuBotSettings(allowed_open_ids=[" ou_abc ", "", "ou_def"])
    assert tuned.allowed_open_ids == ["ou_abc", "ou_def"]


def test_feishu_bot_credentials_come_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "data_source: yfinance\ndb_dir: data\ntickers:\n"
        "  SPY: {asset_type: ETF, currency: USD}\nstrategies: {}\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.feishu_app_id == "cli_test"
    assert settings.feishu_app_secret == "secret_test"
    assert settings.feishu_bot.enabled is False


def test_option_flow_display_switches_default_on() -> None:
    settings = OptionFlowSettings()
    assert settings.display_dedupe_underlying is True
    assert settings.display_sort_by_expiry is True
    tuned = OptionFlowSettings(
        display_dedupe_underlying=False, display_sort_by_expiry=False
    )
    assert tuned.display_dedupe_underlying is False
    assert tuned.display_sort_by_expiry is False


def test_option_flow_normalizes_roots_and_threshold_order() -> None:
    settings = OptionFlowSettings(
        excluded_index_roots=["spx", "vix"],
        etf_roots=["spy", "qqq"],
    )
    assert settings.excluded_index_roots == ["SPX", "VIX"]
    assert settings.etf_roots == ["SPY", "QQQ"]
    with pytest.raises(ValidationError, match="zero_dte_surge_volume"):
        OptionFlowSettings(surge_volume=20_000, zero_dte_surge_volume=19_999)


def test_settings_reject_unknown_top_level_and_strategy_fields() -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        Settings(unexpected=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="typo_days"):
        Settings(strategies={"momentum_rotation": {"typo_days": 60}})


def test_strategy_settings_validate_cross_field_windows() -> None:
    with pytest.raises(ValidationError, match="fast window"):
        Settings(strategies={"macd_cross": {"fast": 30, "slow": 20}})


def test_proxy_and_backup_mirror_are_loaded_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEISHU_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("BACKUP_MIRROR_DIR", str(tmp_path / "mirror"))
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("data_source: yfinance\nstrategies: {}\n", encoding="utf-8")

    settings = load_settings(cfg)

    assert settings.feishu_proxy == "http://127.0.0.1:7890"
    assert settings.backup.mirror_dir == str(tmp_path / "mirror")


def test_feishu_proxy_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError, match="FEISHU_PROXY"):
        Settings(feishu_proxy="file:///tmp/socket")
