from __future__ import annotations

from io import BytesIO

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from quant_signal.fear_dca import (
    calculate_etf_metrics,
    calculate_fear_metrics,
    recommend_qqqm,
    recommend_spy,
)
from quant_signal.fear_dca_chart import (
    prepare_fear_chart_series,
    render_fear_dca_chart,
)


def _inputs() -> tuple[pd.Series[float], pd.Series[float]]:
    sessions = pd.bdate_range("2026-03-02", periods=119)
    steps = np.arange(119, dtype=float)
    vix = pd.Series(22.0 + steps * 0.18 + np.sin(steps / 4.0), index=sessions)
    vxn = pd.Series(31.0 + steps * 0.22 + np.cos(steps / 5.0), index=sessions)
    return vix, vxn


def test_chart_series_display_latest_60_sessions_with_complete_ma60() -> None:
    vix, _ = _inputs()

    series = prepare_fear_chart_series(vix)

    assert len(series.closes) == 60
    assert series.closes.index.equals(vix.tail(60).index)
    assert series.ma20.notna().sum() == 60
    assert series.ma60.notna().sum() == 60


def _render(vix: pd.Series[float], vxn: pd.Series[float]) -> bytes:
    etf = calculate_etf_metrics(pd.Series(np.linspace(110.0, 100.0, 21)))
    vix_metrics = calculate_fear_metrics(vix)
    vxn_metrics = calculate_fear_metrics(vxn)
    return render_fear_dca_chart(
        vix_closes=vix,
        vxn_closes=vxn,
        vix_metrics=vix_metrics,
        vxn_metrics=vxn_metrics,
        spy_decision=recommend_spy(vix_metrics, etf),
        qqqm_decision=recommend_qqqm(vxn_metrics, etf),
    )


def test_chart_is_a_deterministic_nonempty_png_with_fixed_dimensions() -> None:
    vix, vxn = _inputs()

    first = _render(vix, vxn)
    second = _render(vix, vxn)

    assert first == second
    assert len(first) > 10_000
    with Image.open(BytesIO(first)) as image:
        assert image.format == "PNG"
        assert image.size == (1200, 1000)
        assert image.getbbox() == (0, 0, 1200, 1000)


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        (
            "short",
            "at least 119 sessions.*recommendations require only 60",
        ),
        ("misaligned", "must be aligned"),
        ("nonfinite", "finite positive values"),
    ],
)
def test_chart_rejects_invalid_or_unaligned_close_series(
    problem: str, message: str
) -> None:
    vix, vxn = _inputs()
    vix_metrics = calculate_fear_metrics(vix)
    vxn_metrics = calculate_fear_metrics(vxn)
    etf = calculate_etf_metrics(pd.Series(np.linspace(110.0, 100.0, 21)))
    spy_decision = recommend_spy(vix_metrics, etf)
    qqqm_decision = recommend_qqqm(vxn_metrics, etf)
    if problem == "short":
        vxn = vxn.tail(118)
    elif problem == "misaligned":
        vxn.index = pd.bdate_range("2026-03-03", periods=119)
    else:
        vxn.iloc[-3] = np.nan

    with pytest.raises(ValueError, match=message):
        render_fear_dca_chart(
            vix_closes=vix,
            vxn_closes=vxn,
            vix_metrics=vix_metrics,
            vxn_metrics=vxn_metrics,
            spy_decision=spy_decision,
            qqqm_decision=qqqm_decision,
        )
