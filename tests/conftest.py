import numpy as np
import pandas as pd
import pytest

from quant_signal.config import EnrichmentSettings, Settings, TrendGateSettings


def make_test_settings(
    universe: list[str] | None = None,
    watchlist: list[str] | None = None,
    international_tickers: dict[str, str] | None = None,
    **updates: object,
) -> Settings:
    """Build behavioral-test settings without reading production YAML or .env."""
    symbols = universe or ["AAA", "BBB", "CCC", "DDD"]
    intl = international_tickers or {}
    asset_type = {ticker: "STOCK" for ticker in symbols if ticker not in intl}
    data: dict[str, object] = {
        "data_source": "alpaca",
        "db_dir": "data",
        "universe": symbols,
        "watchlist": watchlist if watchlist is not None else ["AAA"],
        "strategies": {
            "momentum_rotation": {
                "lookback_days": 60,
                "top_n": 2,
                "min_dollar_volume": 50_000_000,
            },
            "breakout_20d": {"high_lookback_days": 20, "volume_multiplier": 1.5},
            "price_deviation": {"threshold": 0.02},
            "rsi_reversion": {"period": 14, "oversold": 30, "overbought": 70},
            "macd_cross": {"fast": 12, "slow": 26, "signal": 9},
            "bollinger_breakout": {"period": 20, "num_std": 2.0},
        },
        "asset_type": asset_type,
        "international_tickers": intl,
        "trend_gate": TrendGateSettings(enabled=False),
        "enrichment": EnrichmentSettings(enabled=False),
    }
    data.update(updates)
    return Settings(**data)  # type: ignore[arg-type]


@pytest.fixture
def daily_bars() -> pd.DataFrame:
    """4 只票 × 100 交易日。60 日动量：A(+60%) > B(+30%) > C(+5%) > D(+100% 但成交额过小)。"""
    ts = pd.date_range("2025-08-01", periods=100, freq="B", tz="UTC")
    n = len(ts)
    spec = {  # (日收益率, 日成交量)
        "AAA": (0.008, 5_000_000),
        "BBB": (0.004, 5_000_000),
        "CCC": (0.001, 5_000_000),
        "DDD": (0.012, 1_000),      # 20日均成交额 << $50M
    }
    frames = []
    for t, (r, vol) in spec.items():
        close = 100.0 * np.cumprod(np.full(n, 1 + r))
        df = pd.DataFrame(
            {
                "open": close * 0.999,
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": vol,
            },
            index=pd.MultiIndex.from_product([[t], ts], names=["ticker", "ts"]),
        )
        frames.append(df)
    return pd.concat(frames).sort_index()
