from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_index_names() -> list[Literal["sp500", "nasdaq100"]]:
    return ["sp500", "nasdaq100"]


class NotifySettings(BaseModel):
    dedup_hours: int = 4
    hourly_limit: int = 10
    premarket_hourly_limit: int = 10
    intraday_hourly_limit: int = 10
    deviation_hourly_limit: int = 10
    action_card_only: bool = False

    @model_validator(mode="after")
    def expand_legacy_hourly_limit(self) -> Self:
        for field in (
            "premarket_hourly_limit",
            "intraday_hourly_limit",
            "deviation_hourly_limit",
        ):
            if field not in self.model_fields_set:
                setattr(self, field, self.hourly_limit)
        return self


class TrendGateSettings(BaseModel):
    enabled: bool = False
    ma_days: int = 200
    mom_days: int = 252
    buffer: float = 0.03
    # 回测(阶段B)显示"仅200线+防御"最优，绝对动量/相对强弱过度保守，默认关闭
    use_mom: bool = False
    benchmark: str = "SPY"
    defensive: list[str] = ["BIL", "TLT", "GLD"]
    # 卡片展示的"参考卖出价"用 ATR 吊灯移动止损（贴近价格），与内部200线闸门分工
    chandelier_lookback: int = 22
    chandelier_atr: int = 14
    chandelier_mult: float = 3.0
    # 止盈目标用波动率期望位：现价×(1+日波动×√horizon)，vol_lookback 日算波动
    tp_vol_lookback: int = 60
    tp_horizon: int = 20


class EnrichmentSettings(BaseModel):
    enabled: bool = False
    uzi_run_py: str = ""
    python_exe: str = "python"
    depth: str = "lite"
    timeout_seconds: int = 60
    max_tickers: int = 8


class AIBriefingSettings(BaseModel):
    enabled: bool = False
    provider: Literal["claude_code_cli", "codex_cli"] = "codex_cli"
    command: str = ""
    timeout_seconds: int = 90
    max_chars: int = 6000


class IndexUniverseSettings(BaseModel):
    enabled: bool = False
    indices: list[Literal["sp500", "nasdaq100"]] = Field(
        default_factory=_default_index_names,
        min_length=1,
    )
    cache_path: str = "data/index_universe.json"
    refresh_days: int = Field(default=7, ge=1, le=30)
    max_stale_days: int = Field(default=14, ge=1, le=60)
    scan_top_n: int = Field(default=20, ge=5, le=50)
    execution_top_n: int = Field(default=5, ge=1, le=10)
    min_coverage: float = Field(default=0.98, ge=0.8, le=1.0)
    min_dollar_volume: float = Field(default=50_000_000, gt=0)

    @model_validator(mode="after")
    def validate_windows_and_pool_sizes(self) -> Self:
        if len(set(self.indices)) != len(self.indices):
            raise ValueError("indices must not contain duplicates")
        if self.max_stale_days < self.refresh_days:
            raise ValueError("max_stale_days must be greater than or equal to refresh_days")
        if self.execution_top_n > self.scan_top_n:
            raise ValueError("execution_top_n must not exceed scan_top_n")
        return self


class ExecutionPlanSettings(BaseModel):
    enabled: bool = False
    account_provider: Literal["alpaca_paper", "screenshot", "none"] = "alpaca_paper"
    capital_limit_usd: float = Field(default=6000, gt=0, le=6000)
    max_financing_ratio: float = Field(default=0.20, ge=0, le=0.20)
    screenshot_max_age_hours: int = Field(default=72, ge=1, le=168)
    risk_per_trade: float = Field(default=0.005, gt=0, le=0.05)
    max_daily_new_risk: float = Field(default=0.01, gt=0, le=0.10)
    max_position_weight: float = Field(default=0.12, gt=0, le=0.50)
    max_cluster_weight: float = Field(default=0.35, gt=0, le=1.0)
    cash_reserve: float = Field(default=0.20, ge=0, lt=1.0)
    risk_clusters: dict[str, list[str]] = Field(default_factory=dict)
    max_new_positions_per_day: int = Field(default=2, ge=1, le=10)
    min_stop_distance: float = Field(default=0.02, gt=0, lt=1.0)
    max_stop_distance: float = Field(default=0.20, gt=0, lt=1.0)
    quote_max_age_seconds: int = Field(default=420, ge=60, le=1800)
    account_max_age_seconds: int = Field(default=60, ge=10, le=600)

    @model_validator(mode="after")
    def validate_stop_distances(self) -> Self:
        if self.max_stop_distance <= self.min_stop_distance:
            raise ValueError("max_stop_distance must be greater than min_stop_distance")
        normalized: dict[str, list[str]] = {}
        owners: dict[str, str] = {}
        for cluster, symbols in self.risk_clusters.items():
            clean = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
            if len(clean) != len(set(clean)):
                raise ValueError(f"risk_clusters contains duplicates in {cluster}")
            for symbol in clean:
                previous = owners.get(symbol)
                if previous is not None:
                    raise ValueError(
                        f"risk_clusters symbol {symbol} belongs to both {previous} and {cluster}"
                    )
                owners[symbol] = cluster
            normalized[cluster] = clean
        self.risk_clusters = normalized
        return self


class LegacyPriceDeviationSettings(BaseModel):
    enabled: bool = False


class TickerSettings(BaseModel):
    asset_type: Literal["ETF", "STOCK"]
    currency: str = "USD"
    # 杠杆倍数(产品构造事实, 如 2x 日内杠杆 ETF 填 2)。建议仓位按 等权÷倍数 归一,
    # 让同一份权重承担的风险与 1x 标的对齐。回测: research/backtest_lev_adjust.py
    leverage: float = 1.0


class Settings(BaseModel):
    data_source: Literal["yfinance", "alpaca"] = "yfinance"
    db_dir: str = "data"
    tickers: dict[str, TickerSettings] = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    strategies: dict[str, dict[str, float | int]]
    leverage_factor: dict[str, float] = Field(default_factory=dict)   # 由 tickers.leverage 派生
    momentum_group_top_n: dict[str, int] = Field(default_factory=dict)
    momentum_default_group_top_n: dict[str, int] = Field(default_factory=dict)
    asset_type: dict[str, str] = Field(default_factory=dict)
    international_tickers: dict[str, str] = Field(default_factory=dict)
    notify: NotifySettings = NotifySettings()
    enrichment: EnrichmentSettings = EnrichmentSettings()
    ai_briefing: AIBriefingSettings = AIBriefingSettings()
    trend_gate: TrendGateSettings = TrendGateSettings()
    index_universe: IndexUniverseSettings = IndexUniverseSettings()
    execution_plan: ExecutionPlanSettings = ExecutionPlanSettings()
    legacy_price_deviation: LegacyPriceDeviationSettings = LegacyPriceDeviationSettings()
    # 凭证来自 .env，不出现在 yaml
    alpaca_key: str = ""
    alpaca_secret: str = ""
    feishu_webhook: str = ""

    @model_validator(mode="after")
    def validate_universe_classification(self) -> Self:
        if self.tickers:
            self.universe = list(self.tickers)
            self.asset_type = {
                ticker: metadata.asset_type for ticker, metadata in self.tickers.items()
            }
            self.international_tickers = {
                ticker: metadata.currency
                for ticker, metadata in self.tickers.items()
                if metadata.currency != "USD"
            }
            self.leverage_factor = {
                ticker: metadata.leverage
                for ticker, metadata in self.tickers.items()
                if metadata.leverage != 1.0
            }
        bad_leverage = sorted(t for t, v in self.leverage_factor.items() if v < 1.0)
        if bad_leverage:
            raise ValueError(f"leverage 必须 ≥1: {', '.join(bad_leverage)}")
        classified = set(self.asset_type) | set(self.international_tickers)
        missing = sorted(set(self.universe) - classified)
        if missing:
            raise ValueError(
                "universe 标的缺少分类；美股需 asset_type，外币标的需 "
                f"international_tickers: {', '.join(missing)}"
            )
        invalid_types = sorted(
            ticker for ticker, kind in self.asset_type.items() if kind not in {"ETF", "STOCK"}
        )
        if invalid_types:
            raise ValueError(f"asset_type 必须为 ETF 或 STOCK: {', '.join(invalid_types)}")
        empty_currencies = sorted(
            ticker for ticker, currency in self.international_tickers.items() if not currency
        )
        if empty_currencies:
            raise ValueError(f"国际标的缺少币种: {', '.join(empty_currencies)}")
        return self

    @property
    def db_path(self) -> Path:
        p = REPO_ROOT / self.db_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or REPO_ROOT / "config" / "settings.yaml"
    load_dotenv(REPO_ROOT / "config" / ".env")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["alpaca_key"] = os.environ.get("ALPACA_KEY", "")
    raw["alpaca_secret"] = os.environ.get("ALPACA_SECRET", "")
    raw["feishu_webhook"] = os.environ.get("FEISHU_WEBHOOK", "")
    return Settings(**raw)
