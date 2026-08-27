from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    output_max_chars: int = Field(default=180, ge=60, le=500)


class DataQASettings(BaseModel):
    """Independent cross-provider data-quality service-level objectives."""

    min_coverage: float = Field(default=0.98, ge=0.8, le=1.0)
    divergence_threshold: float = Field(default=0.005, gt=0, le=0.10)
    batch_size: int = Field(default=50, ge=10, le=200)
    lookback_days: int = Field(default=7, ge=3, le=30)


class ForwardEvaluationSettings(BaseModel):
    """Immutable post-publication candidate outcome measurements."""

    horizons: tuple[int, ...] = (5, 10, 20, 21, 63)
    benchmark: str = "QQQ"
    transaction_cost_bps_per_side: float = Field(default=5.0, ge=0, le=100)

    @model_validator(mode="after")
    def normalize_horizons_and_benchmark(self) -> Self:
        horizons = tuple(sorted(set(self.horizons)))
        if not horizons or any(value < 1 or value > 252 for value in horizons):
            raise ValueError("forward evaluation horizons must be between 1 and 252")
        benchmark = self.benchmark.strip().upper()
        if not benchmark:
            raise ValueError("forward evaluation benchmark must not be empty")
        self.horizons = horizons
        self.benchmark = benchmark
        return self


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


class MarketRegimeSettings(BaseModel):
    benchmark: str = "QQQ"
    min_history_days: int = Field(default=201, ge=60, le=500)
    trend_breadth_min: float = Field(default=0.60, ge=0, le=1)
    risk_off_breadth_max: float = Field(default=0.35, ge=0, le=1)
    high_volatility_annualized: float = Field(default=0.35, gt=0, le=2)
    atr_days: int = Field(default=14, ge=5, le=60)
    volatility_days: int = Field(default=20, ge=5, le=120)


class CandidateLaneSettings(BaseModel):
    top_n_per_lane: int = Field(default=3, ge=1, le=10)
    minimum_price: float = Field(default=5.0, gt=0)
    min_dollar_volume: float = Field(default=50_000_000, gt=0)
    overheat_atr_multiple: float = Field(default=2.5, gt=0, le=10)
    pullback_atr_multiple: float = Field(default=1.25, gt=0, le=5)
    stop_atr_multiple: float = Field(default=2.0, gt=0, le=10)
    target_reward_risk: float = Field(default=2.0, gt=1, le=10)
    earnings_blackout_days: int = Field(default=2, ge=0, le=14)
    max_candidates_per_cluster: int = Field(default=2, ge=1, le=10)
    sector_quality_filter_enabled: bool = True
    min_market_cap_usd: int = Field(default=100_000_000_000, ge=1)
    top_n_per_sector: int = Field(default=3, ge=1, le=10)
    max_sectors: int = Field(default=5, ge=1, le=11)
    candidate_group_top_n_overrides: dict[str, int] = Field(
        default_factory=lambda: {"Semiconductors": 10, "Technology": 10}
    )
    profile_max_age_days: int = Field(default=7, ge=1, le=30)
    profile_failure_ttl_hours: int = Field(default=6, ge=1, le=24)
    ai_company_max_chars: int = Field(default=220, ge=80, le=500)
    ai_news_lookback_days: int = Field(default=7, ge=1, le=30)
    ai_news_max_items: int = Field(default=5, ge=0, le=10)
    ai_company_batch_size: int = Field(default=8, ge=1, le=10)
    ai_company_total_timeout_seconds: int = Field(default=120, ge=15, le=600)


class ProfitStageSettings(BaseModel):
    gain: float = Field(gt=0, le=2)
    cumulative_sell: float = Field(gt=0, le=1)


def _default_profit_ladders() -> dict[str, list[ProfitStageSettings]]:
    return {
        "1x": [
            ProfitStageSettings(gain=0.15, cumulative_sell=0.25),
            ProfitStageSettings(gain=0.20, cumulative_sell=0.50),
            ProfitStageSettings(gain=0.30, cumulative_sell=0.75),
        ],
        "2x": [
            ProfitStageSettings(gain=0.10, cumulative_sell=0.25),
            ProfitStageSettings(gain=0.15, cumulative_sell=0.50),
            ProfitStageSettings(gain=0.20, cumulative_sell=0.75),
        ],
        "3x": [
            ProfitStageSettings(gain=0.08, cumulative_sell=0.25),
            ProfitStageSettings(gain=0.12, cumulative_sell=0.50),
            ProfitStageSettings(gain=0.16, cumulative_sell=0.75),
        ],
    }


class PositionDisciplineSettings(BaseModel):
    profit_ladders: dict[str, list[ProfitStageSettings]] = Field(
        default_factory=_default_profit_ladders
    )
    hard_loss_caps: dict[str, float] = Field(
        default_factory=lambda: {"1x": 0.08, "2x": 0.05, "3x": 0.035}
    )
    breakeven_trigger: float = Field(default=0.10, gt=0, le=1)
    stage_one_profit_lock: float = Field(default=0.02, ge=0, le=1)
    stage_two_profit_lock: float = Field(default=0.08, ge=0, le=1)
    trailing_atr_multiple_1x: float = Field(default=3.0, gt=0, le=10)
    trailing_atr_multiple_leveraged: float = Field(default=2.0, gt=0, le=10)
    allow_financing_for_leveraged: bool = False
    max_single_effective_weight: float = Field(default=0.25, gt=0, le=1)
    max_total_leveraged_effective_weight: float = Field(default=0.50, gt=0, le=2)

    @model_validator(mode="after")
    def validate_ladders(self) -> Self:
        for key in ("1x", "2x", "3x"):
            stages = self.profit_ladders.get(key)
            if not stages or key not in self.hard_loss_caps:
                raise ValueError(f"position discipline requires {key} rules")
            if any(
                later.gain <= earlier.gain
                or later.cumulative_sell <= earlier.cumulative_sell
                for earlier, later in zip(stages, stages[1:])
            ):
                raise ValueError(f"profit ladder {key} must increase monotonically")
            cap = self.hard_loss_caps[key]
            if cap <= 0 or cap >= 1:
                raise ValueError(f"hard loss cap {key} must be between zero and one")
        return self


class USBriefingSettings(BaseModel):
    enabled: bool = False
    delivery_mode: Literal["shadow", "live"] = "shadow"
    candidate_index: Literal["nasdaq100"] = "nasdaq100"
    min_coverage: float = Field(default=0.98, ge=0.8, le=1)
    morning_hour_utc: int = Field(default=0, ge=0, le=23)
    morning_minute_utc: int = Field(default=0, ge=0, le=59)
    afternoon_hour_utc: int = Field(default=7, ge=0, le=23)
    afternoon_minute_utc: int = Field(default=30, ge=0, le=59)
    market_regime: MarketRegimeSettings = MarketRegimeSettings()
    candidate_lanes: CandidateLaneSettings = CandidateLaneSettings()
    position_discipline: PositionDisciplineSettings = PositionDisciplineSettings()


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


def _default_option_venues() -> list[Literal["cone", "ctwo", "opt", "exo"]]:
    return ["cone", "ctwo", "opt", "exo"]


class OptionFlowSettings(BaseModel):
    enabled: bool = False
    feed: Literal["indicative", "opra"] = "indicative"
    top_n: int = Field(default=10, ge=1, le=20)
    discovery_limit: int = Field(default=50, ge=10, le=100)
    venues: list[Literal["cone", "ctwo", "opt", "exo"]] = Field(
        default_factory=_default_option_venues,
        min_length=1,
    )
    excluded_index_roots: list[str] = Field(
        default_factory=lambda: [
            "SPX", "SPXW", "VIX", "RUT", "RUTW", "NDX", "XSP", "OEX"
        ]
    )
    etf_roots: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    min_volume: int = Field(default=5000, ge=0)
    surge_volume: int = Field(default=10_000, ge=1)
    zero_dte_surge_volume: int = Field(default=20_000, ge=1)
    rank_jump: int = Field(default=3, ge=1, le=20)
    cooldown_minutes: int = Field(default=60, ge=15, le=240)
    max_alerts_per_day: int = Field(default=4, ge=2, le=12)
    intraday_expiry_minutes: int = Field(default=45, ge=15, le=180)
    closing_expiry_hours: int = Field(default=12, ge=1, le=24)
    retention_days: int = Field(default=120, ge=30)
    min_venue_coverage: float = Field(default=0.75, ge=0.5, le=1.0)
    circuit_breaker_failures: int = Field(default=2, ge=1, le=10)
    circuit_breaker_cooldown_minutes: int = Field(default=10, ge=1, le=60)
    # 展示层开关：只影响卡片显示，不改变排名/异动/落库口径
    display_dedupe_underlying: bool = True
    display_sort_by_expiry: bool = True
    holding_monitor_enabled: bool = True
    holding_max_expiry_days: int = Field(default=14, ge=1, le=60)
    holding_max_tickers: int = Field(default=12, ge=1, le=50)
    holding_min_delta_volume: int = Field(default=5_000, ge=0)
    holding_dominance_threshold: float = Field(default=0.20, ge=0, le=1)
    holding_cooldown_minutes: int = Field(default=60, ge=15, le=240)
    holding_max_alerts_per_day: int = Field(default=4, ge=1, le=12)

    @model_validator(mode="after")
    def validate_option_flow_policy(self) -> Self:
        if self.discovery_limit < self.top_n:
            raise ValueError("discovery_limit must be greater than or equal to top_n")
        if self.zero_dte_surge_volume < self.surge_volume:
            raise ValueError(
                "zero_dte_surge_volume must be greater than or equal to surge_volume"
            )
        if len(self.venues) != len(set(self.venues)):
            raise ValueError("venues must not contain duplicates")
        expected = {"cone", "ctwo", "opt", "exo"}
        if self.enabled and set(self.venues) != expected:
            raise ValueError("venues must include cone, ctwo, opt, and exo when enabled")
        self.excluded_index_roots = [
            root.strip().upper() for root in self.excluded_index_roots if root.strip()
        ]
        self.etf_roots = [root.strip().upper() for root in self.etf_roots if root.strip()]
        if len(self.excluded_index_roots) != len(set(self.excluded_index_roots)):
            raise ValueError("excluded_index_roots must not contain duplicates")
        if len(self.etf_roots) != len(set(self.etf_roots)):
            raise ValueError("etf_roots must not contain duplicates")
        return self


class OptionIntelSettings(BaseModel):
    """持仓期权情报层（只观察，不推荐任何期权交易）。默认关闭。"""

    enabled: bool = False
    max_expiry_days: int = Field(default=60, ge=7, le=120)
    min_expected_move_dte: int = Field(default=7, ge=1, le=30)
    top_oi_strikes: int = Field(default=3, ge=1, le=5)
    iv_rv_warn_ratio: float = Field(default=1.5, gt=1.0)
    retention_days: int = Field(default=400, ge=90)
    max_tickers: int = Field(default=12, ge=1, le=30)
    wall_high_min_oi_each_side: int = Field(default=5_000, ge=1)
    wall_medium_min_oi_each_side: int = Field(default=1_000, ge=1)
    wall_high_min_oi_coverage: float = Field(default=0.80, ge=0, le=1)
    wall_medium_min_oi_coverage: float = Field(default=0.50, ge=0, le=1)
    wall_high_min_quote_coverage: float = Field(default=0.60, ge=0, le=1)
    wall_high_min_concentration: float = Field(default=0.10, ge=0, le=1)
    gamma_near_spot_pct: float = Field(default=0.02, gt=0, le=0.20)

    @model_validator(mode="after")
    def validate_tactical_thresholds(self) -> Self:
        if self.wall_high_min_oi_each_side < self.wall_medium_min_oi_each_side:
            raise ValueError("high wall OI threshold must be at least medium")
        if self.wall_high_min_oi_coverage < self.wall_medium_min_oi_coverage:
            raise ValueError("high wall OI coverage must be at least medium")
        return self


class PriceMoveCauseSearchSettings(BaseModel):
    """Codex 联网搜索异动原因；失败时告警仍必须发出。"""

    enabled: bool = False
    command: str = "codex"
    model: str = "gpt-5.6-terra"
    reasoning_effort: Literal["low", "medium"] = "low"
    timeout_seconds: int = Field(default=60, ge=5, le=120)
    lookback_hours: int = Field(default=24, ge=1, le=168)
    max_sources: int = Field(default=3, ge=1, le=5)
    max_alerts_per_batch: int = Field(default=8, ge=1, le=10)
    max_summary_chars: int = Field(default=220, ge=60, le=500)


class HoldingPriceAlertSettings(BaseModel):
    """真实持仓的分钟级股价异动监控。

    固定涨跌幅是最低门槛；实际门槛会根据最近的分钟波动率自适应抬高，
    避免高波动个股在正常噪声中频繁告警。
    """

    enabled: bool = False
    lookback_minutes: int = Field(default=45, ge=20, le=240)
    max_bar_age_seconds: int = Field(default=120, ge=30, le=600)
    stock_1m_pct: float = Field(default=0.020, gt=0, le=0.20)
    stock_5m_pct: float = Field(default=0.040, gt=0, le=0.30)
    stock_15m_pct: float = Field(default=0.060, gt=0, le=0.50)
    stock_session_pct: float = Field(default=0.100, gt=0, le=1.0)
    etf_1m_pct: float = Field(default=0.012, gt=0, le=0.20)
    etf_5m_pct: float = Field(default=0.025, gt=0, le=0.30)
    etf_15m_pct: float = Field(default=0.040, gt=0, le=0.50)
    etf_session_pct: float = Field(default=0.060, gt=0, le=1.0)
    volatility_sigma_multiple_1m: float = Field(default=4.0, gt=0, le=20)
    volatility_sigma_multiple_5m: float = Field(default=3.0, gt=0, le=20)
    volatility_sigma_multiple_15m: float = Field(default=2.5, gt=0, le=20)
    volatility_sigma_multiple_session: float = Field(default=2.0, gt=0, le=20)
    volume_spike_multiple: float = Field(default=4.0, gt=1, le=50)
    min_volume_spike_move_pct: float = Field(default=0.010, gt=0, le=0.10)
    cooldown_minutes: int = Field(default=30, ge=5, le=240)
    max_alerts_per_day: int = Field(default=5, ge=1, le=50)
    regular_alert_slots: int = Field(default=4, ge=0, le=50)
    max_alerts_per_ticker_per_day: int = Field(default=2, ge=1, le=10)
    meaningful_upgrade_score: float = Field(default=1.5, ge=1.0, le=10.0)
    max_tickers: int = Field(default=20, ge=1, le=100)
    cause_search: PriceMoveCauseSearchSettings = PriceMoveCauseSearchSettings()

    @model_validator(mode="after")
    def validate_daily_quota_slots(self) -> Self:
        if self.regular_alert_slots > self.max_alerts_per_day:
            raise ValueError(
                "holding_price_alert.regular_alert_slots cannot exceed "
                "max_alerts_per_day"
            )
        return self


class ExtremeMoverSettings(BaseModel):
    """Full-market daily ±10% event collection and ranking."""

    enabled: bool = False
    feed: Literal["hybrid", "sip"] = "hybrid"
    threshold: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("1"))
    screen_threshold: Decimal = Field(default=Decimal("0.08"), gt=0, le=Decimal("1"))
    min_price: Decimal = Field(default=Decimal("5"), ge=0)
    min_dollar_volume: Decimal = Field(default=Decimal("20000000"), ge=0)
    windows: tuple[int, ...] = (30,)
    default_window: int = 30
    chunk_size: int = Field(default=200, ge=1, le=500)
    confirmation_chunk_size: int = Field(default=50, ge=1, le=200)
    deadline_seconds: int = Field(default=600, ge=60, le=3600)
    min_coverage: float = Field(default=0.90, gt=0, le=1)
    min_confirmation_coverage: float = Field(default=0.95, gt=0, le=1)
    top_stocks: int = Field(default=10, ge=1, le=50)
    top_sectors: int = Field(default=5, ge=1, le=11)
    close_hour_et: int = Field(default=16, ge=0, le=23)
    close_minute_et: int = Field(default=30, ge=0, le=59)
    premarket_hour_et: int = Field(default=8, ge=0, le=23)
    premarket_minute_et: int = Field(default=0, ge=0, le=59)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if not self.windows or any(window < 1 or window > 504 for window in self.windows):
            raise ValueError("extreme mover windows must be between 1 and 504")
        if self.default_window not in self.windows:
            raise ValueError("default_window must be included in windows")
        if self.screen_threshold > self.threshold:
            raise ValueError("screen_threshold cannot exceed threshold")
        return self


class LegacyPriceDeviationSettings(BaseModel):
    enabled: bool = False


class TickerSettings(BaseModel):
    asset_type: Literal["ETF", "STOCK"]
    currency: str = "USD"
    # 杠杆倍数(产品构造事实, 如 2x 日内杠杆 ETF 填 2)。建议仓位按 等权÷倍数 归一,
    # 让同一份权重承担的风险与 1x 标的对齐。回测: research/backtest_lev_adjust.py
    leverage: float = 1.0


class MomentumStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=60, ge=2, le=500)
    top_n: int = Field(default=3, ge=1, le=100)
    min_dollar_volume: float = Field(default=50_000_000, ge=0)


class BreakoutStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    high_lookback_days: int = Field(default=20, ge=2, le=252)
    volume_multiplier: float = Field(default=1.5, gt=0, le=20)


class PriceDeviationStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(default=0.02, gt=0, le=1)


class RSIStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: int = Field(default=14, ge=2, le=100)
    oversold: float = Field(default=30, ge=0, lt=50)
    overbought: float = Field(default=70, gt=50, le=100)

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        if self.oversold >= self.overbought:
            raise ValueError("RSI oversold must be below overbought")
        return self


class MACDStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fast: int = Field(default=12, ge=1, le=200)
    slow: int = Field(default=26, ge=2, le=400)
    signal: int = Field(default=9, ge=1, le=200)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if self.fast >= self.slow:
            raise ValueError("MACD fast window must be below slow window")
        return self


class BollingerStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: int = Field(default=20, ge=2, le=252)
    num_std: float = Field(default=2.0, gt=0, le=10)


class StrategiesSettings(BaseModel):
    """Typed strategy configuration with mapping compatibility for research scripts."""

    model_config = ConfigDict(extra="forbid")

    momentum_rotation: MomentumStrategySettings = MomentumStrategySettings()
    breakout_20d: BreakoutStrategySettings = BreakoutStrategySettings()
    price_deviation: PriceDeviationStrategySettings = PriceDeviationStrategySettings()
    rsi_reversion: RSIStrategySettings = RSIStrategySettings()
    macd_cross: MACDStrategySettings = MACDStrategySettings()
    bollinger_breakout: BollingerStrategySettings = BollingerStrategySettings()

    def __getitem__(self, name: str) -> dict[str, float | int]:
        value = getattr(self, name)
        if not isinstance(value, BaseModel):
            raise KeyError(name)
        return value.model_dump()

    def get(
        self, name: str, default: dict[str, float | int] | None = None
    ) -> dict[str, float | int]:
        try:
            return self[name]
        except (AttributeError, KeyError):
            return default or {}


class BackupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_days: int = Field(default=14, ge=1, le=365)
    mirror_dir: str = ""
    require_mirror: bool = False

    @model_validator(mode="after")
    def validate_mirror(self) -> Self:
        self.mirror_dir = self.mirror_dir.strip()
        if self.require_mirror and not self.mirror_dir:
            raise ValueError("backup.mirror_dir is required when require_mirror=true")
        return self


class FeishuBotSettings(BaseModel):
    """自建应用机器人交互（长连接接收单聊消息）。默认关闭。"""

    enabled: bool = False
    allowed_open_ids: list[str] = Field(default_factory=list)
    # 主推送通道目标：ou_ 开头推单聊，oc_ 开头推群；留空则推送继续走 webhook
    push_receive_id: str = ""
    capital_limit: Decimal = Field(default=Decimal("6000"), gt=0)
    max_financing_ratio: Decimal = Field(default=Decimal("0.20"), ge=0)
    confirm_window_minutes: int = Field(default=15, ge=1, le=120)
    codex_timeout_seconds: float = Field(default=180, gt=0)

    @model_validator(mode="after")
    def normalize_open_ids(self) -> Self:
        self.allowed_open_ids = [
            open_id.strip() for open_id in self.allowed_open_ids if open_id.strip()
        ]
        self.push_receive_id = self.push_receive_id.strip()
        return self


class FearDcaSettings(BaseModel):
    """Operational settings for the fear-index DCA reminder."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    hour: int = Field(default=9, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)
    vix_symbol: str = "^VIX"
    vxn_symbol: str = "^VXN"
    spy_symbol: str = "SPY"
    qqqm_symbol: str = "QQQM"
    lookback_calendar_days: int = Field(default=220, ge=180)
    chart_sessions: Literal[60] = 60
    source_label: str = "Yahoo Finance"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        timezone_name = value.strip()
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(f"unknown timezone: {timezone_name}") from error
        return timezone_name

    @model_validator(mode="after")
    def normalize_data_contract(self) -> Self:
        symbol_fields = ("vix_symbol", "vxn_symbol", "spy_symbol", "qqqm_symbol")
        symbols: list[str] = []
        for field_name in symbol_fields:
            symbol = str(getattr(self, field_name)).strip().upper()
            if not symbol:
                raise ValueError(f"fear_dca.{field_name} must not be empty")
            setattr(self, field_name, symbol)
            symbols.append(symbol)
        if len(set(symbols)) != len(symbols):
            raise ValueError("fear_dca symbols must be unique")
        self.source_label = self.source_label.strip()
        if not self.source_label:
            raise ValueError("fear_dca.source_label must not be empty")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_source: Literal["yfinance", "alpaca"] = "yfinance"
    db_dir: str = "data"
    tickers: dict[str, TickerSettings] = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    strategies: StrategiesSettings = StrategiesSettings()
    leverage_factor: dict[str, float] = Field(default_factory=dict)   # 由 tickers.leverage 派生
    momentum_group_top_n: dict[str, int] = Field(default_factory=dict)
    momentum_default_group_top_n: dict[str, int] = Field(default_factory=dict)
    asset_type: dict[str, str] = Field(default_factory=dict)
    international_tickers: dict[str, str] = Field(default_factory=dict)
    notify: NotifySettings = NotifySettings()
    enrichment: EnrichmentSettings = EnrichmentSettings()
    ai_briefing: AIBriefingSettings = AIBriefingSettings()
    data_qa: DataQASettings = DataQASettings()
    forward_evaluation: ForwardEvaluationSettings = ForwardEvaluationSettings()
    trend_gate: TrendGateSettings = TrendGateSettings()
    index_universe: IndexUniverseSettings = IndexUniverseSettings()
    us_briefing: USBriefingSettings = USBriefingSettings()
    execution_plan: ExecutionPlanSettings = ExecutionPlanSettings()
    option_flow: OptionFlowSettings = OptionFlowSettings()
    option_intel: OptionIntelSettings = OptionIntelSettings()
    holding_price_alert: HoldingPriceAlertSettings = HoldingPriceAlertSettings()
    extreme_movers: ExtremeMoverSettings = ExtremeMoverSettings()
    fear_dca: FearDcaSettings = FearDcaSettings()
    legacy_price_deviation: LegacyPriceDeviationSettings = LegacyPriceDeviationSettings()
    feishu_bot: FeishuBotSettings = FeishuBotSettings()
    backup: BackupSettings = BackupSettings()
    # 凭证来自 .env，不出现在 yaml
    alpaca_key: str = ""
    alpaca_secret: str = ""
    feishu_webhook: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_proxy: str = ""

    @field_validator("feishu_proxy")
    @classmethod
    def validate_feishu_proxy(cls, value: str) -> str:
        proxy = value.strip()
        if not proxy:
            return ""
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            raise ValueError(
                "FEISHU_PROXY must be an http(s) or socks5 proxy URL"
            )
        return proxy

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
    raw["feishu_app_id"] = os.environ.get("FEISHU_APP_ID", "")
    raw["feishu_app_secret"] = os.environ.get("FEISHU_APP_SECRET", "")
    raw["feishu_proxy"] = os.environ.get("FEISHU_PROXY", "")
    mirror_dir = os.environ.get("BACKUP_MIRROR_DIR", "").strip()
    if mirror_dir:
        raw.setdefault("backup", {})["mirror_dir"] = mirror_dir
    return Settings(**raw)
