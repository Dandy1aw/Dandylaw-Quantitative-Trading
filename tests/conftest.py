import numpy as np
import pandas as pd
import pytest


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
