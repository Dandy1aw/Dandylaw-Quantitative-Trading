from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from quant_signal.config import (
    EnrichmentSettings,
    ExecutionPlanSettings,
    IndexUniverseSettings,
    NotifySettings,
    OptionFlowSettings,
    Settings,
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
    assert set(s.universe) == set(s.tickers)
    assert set(s.watchlist) == {"NVDA", "TSLA", "AAPL", "MSFT", "AMD"}


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
        ("min_venue_coverage", 0.99),
        ("retention_days", 29),
    ],
)
def test_option_flow_rejects_unsafe_policy(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        OptionFlowSettings(**{field: value})


def test_option_flow_retention_defaults_to_120_days() -> None:
    assert OptionFlowSettings().retention_days == 120


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
