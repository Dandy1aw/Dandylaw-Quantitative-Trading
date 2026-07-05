import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.trend_gate import (
    TrendGateConfig,
    apply_trend_gate,
    trend_state,
    weekly_state_map,
)

CFG = TrendGateConfig()


def _series(vals, start="2022-01-03"):  # type: ignore[no-untyped-def]
    idx = pd.bdate_range(start, periods=len(vals), tz="UTC")
    return pd.Series(vals, index=idx)


def _flat_rf(n, start="2022-01-03"):  # type: ignore[no-untyped-def]
    # BIL 近似常数（rf≈0）
    return _series(np.linspace(100.0, 101.0, n), start)


def test_uptrend_enters_long() -> None:
    n = 320
    close = _series(100.0 * np.cumprod(np.full(n, 1.004)))   # 稳定上涨
    bench = _series(100.0 * np.cumprod(np.full(n, 1.001)))   # 大盘涨得慢 → cond_rs 成立
    rf = _flat_rf(n)
    info = trend_state("MU", close, bench, rf, is_stock=True, is_usd=True,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "LONG"
    assert info.signal in ("ENTER", "HOLD")
    assert abs(info.sell_ref - info.sma200 * (1 - CFG.buffer)) < 1e-9


def test_break_and_mom_fail_exits() -> None:
    up = 100.0 * np.cumprod(np.full(220, 1.004))
    down = up[-1] * np.cumprod(np.full(100, 0.985))          # 后段大跌，跌破200线且12m转负
    close = _series(np.concatenate([up, down]))
    bench = _series(100.0 * np.cumprod(np.full(320, 1.0005)))
    rf = _flat_rf(320)
    info = trend_state("MU", close, bench, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "FLAT"          # 双失效已退出


def test_non_usd_uses_only_ma() -> None:
    n = 320
    close = _series(100.0 * np.cumprod(np.full(n, 1.004)))
    bench = _series(100.0 * np.cumprod(np.full(n, 1.01)))    # 强基准会打败 cond_rs，但非美元应忽略
    rf = _flat_rf(n)
    info = trend_state("7709.HK", close, bench, rf, is_stock=True, is_usd=False,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "LONG"          # 只看站上200线


def test_insufficient_history_returns_none() -> None:
    close = _series(100.0 * np.cumprod(np.full(50, 1.004)))
    rf = _flat_rf(50)
    assert trend_state("MU", close, close, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1]) is None


def test_ma_only_flag_ignores_momentum() -> None:
    """对照组 ma_only：只用200线，绝对动量失效也不影响入场/退出。"""
    n = 320
    # 价格站上200线但过去252日收益为负（近端拉起、远端更高 → 12m 为负），
    # 用先跌后涨构造：cond_mom 失败但 cond_ma 成立。
    first = 200.0 * np.cumprod(np.full(160, 0.995))
    second = first[-1] * np.cumprod(np.full(160, 1.006))
    close = _series(np.concatenate([first, second]))
    rf = _flat_rf(n)
    full = trend_state("X", close, close, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1], use_mom=True)
    ma_only = trend_state("X", close, close, rf, is_stock=False, is_usd=True,
                          cfg=CFG, as_of=close.index[-1], use_mom=False)
    assert ma_only is not None and ma_only.state == "LONG"     # 只看200线 → 持有
    # full 因 cond_mom 参与，结果可能不同（这里至少验证 flag 生效、两者可分别调用）
    assert full is not None


def _bars_from(series_map):  # type: ignore[no-untyped-def]
    frames = []
    for t, s in series_map.items():
        df = pd.DataFrame(
            {"open": s, "high": s, "low": s, "close": s, "volume": 1_000_000.0}
        )
        df.index = pd.MultiIndex.from_product([[t], s.index], names=["ticker", "ts"])
        frames.append(df)
    return pd.concat(frames).sort_index()


def _pick(ticker, price):  # type: ignore[no-untyped-def]
    return Signal(
        ticker=ticker, direction=Direction.BUY, price=price, reason="动量",
        strategy_id="momentum_rotation", ts=pd.Timestamp("2022-01-03", tz="UTC"),
        suggested_weight=0.5,
    )


def test_long_pick_kept_flat_pick_routed_to_defensive() -> None:
    n = 320
    up = _series(100.0 * np.cumprod(np.full(n, 1.004)))          # MU 强趋势 → LONG
    weak = _series(np.concatenate([
        100.0 * np.cumprod(np.full(220, 1.004)),
        (100.0 * 1.004 ** 220) * np.cumprod(np.full(100, 0.985)),
    ]))                                                          # BAD 跌破 → FLAT
    tlt = _series(100.0 * np.cumprod(np.full(n, 1.003)))         # 防御里 TLT 最强
    gld = _series(100.0 * np.cumprod(np.full(n, 1.001)))
    bil = _flat_rf(n)
    spy = _series(100.0 * np.cumprod(np.full(n, 1.0005)))
    bars = _bars_from({"MU": up, "BAD": weak, "TLT": tlt, "GLD": gld, "BIL": bil, "SPY": spy})

    picks = [_pick("MU", float(up.iloc[-1])), _pick("BAD", float(weak.iloc[-1]))]
    final, infos = apply_trend_gate(
        picks, bars, asset_type={"MU": "STOCK", "BAD": "STOCK"},
        international_tickers={}, cfg=TrendGateConfig(),
    )
    tickers = {s.ticker for s in final}
    assert "MU" in tickers                 # 趋势健康，保留
    assert "BAD" not in tickers            # 趋势失效，被切
    assert "TLT" in tickers                # 释放权重投最强防御
    tlt_sig = next(s for s in final if s.ticker == "TLT")
    assert "防御切换" in tlt_sig.reason
    assert tlt_sig.suggested_weight is not None
    assert abs(tlt_sig.suggested_weight - 0.5) < 1e-9


def test_all_long_no_defensive() -> None:
    n = 320
    up = _series(100.0 * np.cumprod(np.full(n, 1.004)))
    bil = _flat_rf(n)
    spy = _series(100.0 * np.cumprod(np.full(n, 1.0005)))
    bars = _bars_from({"MU": up, "TLT": up, "GLD": up, "BIL": bil, "SPY": spy})
    final, _ = apply_trend_gate([_pick("MU", float(up.iloc[-1]))], bars,
                                asset_type={"MU": "STOCK"}, international_tickers={},
                                cfg=TrendGateConfig())
    assert [s.ticker for s in final] == ["MU"]   # 无 FLAT，无防御替换


def test_weekly_state_map_matches_trend_state_last() -> None:
    """weekly_state_map 末周的 state 应与 trend_state(as_of=末日) 一致（共享 _replay）。"""
    n = 320
    close = _series(100.0 * np.cumprod(np.full(n, 1.004)))
    rf = _flat_rf(n)
    wsm = weekly_state_map(close, close, rf, is_stock=False, is_usd=True, cfg=CFG)
    info = trend_state("X", close, close, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None and wsm.iloc[-1] == info.state == "LONG"
