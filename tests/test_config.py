from pydantic import ValidationError

from quant_signal.config import EnrichmentSettings, NotifySettings, Settings, load_settings

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
    # 趋势闸门：阶段B回测选出的最优配置(仅200线+防御，关绝对动量)
    assert s.trend_gate.enabled is True
    assert s.trend_gate.use_mom is False
    assert s.trend_gate.defensive == ["BIL", "TLT", "GLD"]


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
