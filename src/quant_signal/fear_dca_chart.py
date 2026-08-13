"""Deterministic Pillow chart for fear-index DCA reminders."""

from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from typing import NamedTuple, TypeAlias

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from quant_signal.fear_dca import (
    FearMetrics,
    RecommendationDecision,
    calculate_fear_metrics,
    validate_fear_metrics,
)

CHART_WIDTH = 1200
CHART_HEIGHT = 1000
_SESSIONS = 60
_MIN_CHART_SESSIONS = _SESSIONS * 2 - 1

_Font: TypeAlias = ImageFont.FreeTypeFont | ImageFont.ImageFont
_Point: TypeAlias = tuple[float, float]

_BACKGROUND = "#F7F9FC"
_PANEL = "#FFFFFF"
_GRID = "#D9E0EA"
_TEXT = "#172033"
_MUTED = "#667085"
_CLOSE = "#2563EB"
_MA20 = "#F59E0B"
_MA60 = "#7C3AED"
_THRESHOLD = "#DC2626"


class FearChartSeries(NamedTuple):
    """The exact 60-session closes and moving averages drawn in one panel."""

    closes: pd.Series[float]
    ma20: pd.Series[float]
    ma60: pd.Series[float]


def _font(size: int, *, bold: bool = False) -> _Font:
    """Load a CJK-capable font where available and fall back safely."""
    windows = Path("C:/Windows/Fonts")
    candidates = (
        windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
        windows / ("simhei.ttf" if bold else "simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
        if bold
        else Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def _validated_series(label: str, closes: pd.Series[float]) -> pd.Series[float]:
    if not isinstance(closes.index, pd.DatetimeIndex):
        raise TypeError(f"{label} closes must use a DatetimeIndex")
    if not closes.index.is_unique or not closes.index.is_monotonic_increasing:
        raise ValueError(f"{label} closes index must be unique and chronological")
    if len(closes) < _MIN_CHART_SESSIONS:
        raise ValueError(
            f"{label} closes requires at least 119 sessions for a complete "
            "60-point MA60; recommendations require only 60 sessions"
        )
    numeric = pd.to_numeric(closes, errors="coerce").astype(float)
    values = numeric.to_numpy(dtype=float)
    if not bool(np.all(np.isfinite(values) & (values > 0.0))):
        raise ValueError(f"{label} closes must contain finite positive values")
    return numeric


def prepare_fear_chart_series(closes: pd.Series[float]) -> FearChartSeries:
    """Prepare the latest 60 sessions with a valid MA20 and MA60 at every point.

    Chart rendering needs 119 sessions of warm-up history. This is deliberately
    stricter than the independent recommendation calculation's 60-session minimum.
    """
    validated = _validated_series("fear chart", closes)
    return FearChartSeries(
        closes=validated.tail(_SESSIONS),
        ma20=validated.rolling(window=20, min_periods=20).mean().tail(_SESSIONS),
        ma60=validated.rolling(window=60, min_periods=60).mean().tail(_SESSIONS),
    )


def _validate_annotations(
    label: str,
    closes: pd.Series[float],
    metrics: FearMetrics,
    decision: RecommendationDecision,
) -> None:
    validate_fear_metrics(metrics)
    calculated = calculate_fear_metrics(closes)
    for field in ("close", "ma20", "ma60"):
        actual = float(getattr(metrics, field))
        expected = float(getattr(calculated, field))
        if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(f"{label} metrics do not match the close series")
    multipliers = (
        decision.base_multiplier,
        decision.drawdown_bonus,
        decision.final_multiplier,
    )
    if any(not math.isfinite(value) or value < 0.0 for value in multipliers):
        raise ValueError(f"{label} decision contains invalid multipliers")


def _series_points(
    values: pd.Series[float],
    *,
    plot_left: int,
    plot_top: int,
    plot_right: int,
    plot_bottom: int,
    y_min: float,
    y_max: float,
) -> list[_Point | None]:
    x_span = plot_right - plot_left
    y_span = plot_bottom - plot_top
    points: list[_Point | None] = []
    for position, raw_value in enumerate(values.to_numpy(dtype=float)):
        if not math.isfinite(raw_value):
            points.append(None)
            continue
        x = plot_left + position * x_span / (_SESSIONS - 1)
        y = float(plot_bottom - (raw_value - y_min) * y_span / (y_max - y_min))
        points.append((x, y))
    return points


def _draw_segments(
    draw: ImageDraw.ImageDraw,
    points: list[_Point | None],
    *,
    fill: str,
    width: int,
) -> None:
    segment: list[_Point] = []
    for point in points + [None]:
        if point is not None:
            segment.append(point)
        else:
            if len(segment) >= 2:
                draw.line(segment, fill=fill, width=width, joint="curve")
            elif segment:
                x, y = segment[0]
                draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=fill)
            segment = []


def _draw_dashed_horizontal(
    draw: ImageDraw.ImageDraw, x1: int, x2: int, y: float, *, fill: str
) -> None:
    for start in range(x1, x2, 18):
        draw.line((start, y, min(start + 10, x2), y), fill=fill, width=2)


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    *,
    panel_top: int,
    label: str,
    closes: pd.Series[float],
    threshold: float,
    decision: RecommendationDecision,
    title_font: _Font,
    body_font: _Font,
    small_font: _Font,
) -> None:
    panel_left, panel_right = 40, CHART_WIDTH - 40
    panel_bottom = panel_top + 390
    draw.rounded_rectangle(
        (panel_left, panel_top, panel_right, panel_bottom),
        radius=18,
        fill=_PANEL,
        outline="#E5EAF1",
        width=2,
    )
    draw.text((65, panel_top + 20), label, fill=_TEXT, font=title_font)

    prepared = prepare_fear_chart_series(closes)
    window = prepared.closes
    ma20 = prepared.ma20
    ma60 = prepared.ma60
    finite_values = [*window.to_list(), threshold]
    finite_values.extend(float(value) for value in ma20.dropna())
    finite_values.extend(float(value) for value in ma60.dropna())
    low, high = min(finite_values), max(finite_values)
    padding = max((high - low) * 0.10, 1.0)
    y_min, y_max = low - padding, high + padding

    plot_left, plot_right = 90, CHART_WIDTH - 70
    plot_top, plot_bottom = panel_top + 72, panel_bottom - 48
    for line_index in range(5):
        y = plot_top + line_index * (plot_bottom - plot_top) / 4
        value = y_max - line_index * (y_max - y_min) / 4
        draw.line((plot_left, y, plot_right, y), fill=_GRID, width=1)
        draw.text((45, y - 8), f"{value:.1f}", fill=_MUTED, font=small_font)

    threshold_y = plot_bottom - (threshold - y_min) * (plot_bottom - plot_top) / (
        y_max - y_min
    )
    _draw_dashed_horizontal(draw, plot_left, plot_right, threshold_y, fill=_THRESHOLD)

    for series, color, width in (
        (window, _CLOSE, 4),
        (ma20, _MA20, 3),
        (ma60, _MA60, 3),
    ):
        _draw_segments(
            draw,
            _series_points(
                series,
                plot_left=plot_left,
                plot_top=plot_top,
                plot_right=plot_right,
                plot_bottom=plot_bottom,
                y_min=y_min,
                y_max=y_max,
            ),
            fill=color,
            width=width,
        )

    latest_x = float(plot_right)
    latest_value = float(window.iloc[-1])
    latest_y = plot_bottom - (latest_value - y_min) * (plot_bottom - plot_top) / (
        y_max - y_min
    )
    draw.ellipse(
        (latest_x - 6, latest_y - 6, latest_x + 6, latest_y + 6),
        fill=_CLOSE,
        outline=_PANEL,
        width=2,
    )
    latest_date = window.index[-1].strftime("%Y-%m-%d")
    annotation = (
        f"{latest_date}  {latest_value:.2f}  base {decision.base_multiplier:g}x"
    )
    text_box = draw.textbbox((0, 0), annotation, font=body_font)
    text_width = text_box[2] - text_box[0]
    annotation_x = max(plot_left + 5, plot_right - text_width - 12)
    annotation_y = max(plot_top + 5, latest_y - 34)
    draw.rounded_rectangle(
        (
            annotation_x - 7,
            annotation_y - 4,
            annotation_x + text_width + 7,
            annotation_y + 23,
        ),
        radius=6,
        fill="#EFF6FF",
        outline="#BFDBFE",
    )
    draw.text((annotation_x, annotation_y), annotation, fill=_TEXT, font=body_font)

    start_date = window.index[0].strftime("%m-%d")
    end_date = window.index[-1].strftime("%m-%d")
    draw.text((plot_left, plot_bottom + 12), start_date, fill=_MUTED, font=small_font)
    end_box = draw.textbbox((0, 0), end_date, font=small_font)
    draw.text(
        (plot_right - (end_box[2] - end_box[0]), plot_bottom + 12),
        end_date,
        fill=_MUTED,
        font=small_font,
    )
    legend_x = 235.0
    for legend_label, color in (
        ("Close", _CLOSE),
        ("MA20", _MA20),
        ("MA60", _MA60),
        (f"Threshold {threshold:g}", _THRESHOLD),
    ):
        draw.line(
            (legend_x, panel_top + 33, legend_x + 27, panel_top + 33),
            fill=color,
            width=3,
        )
        draw.text(
            (legend_x + 34, panel_top + 22),
            legend_label,
            fill=_MUTED,
            font=small_font,
        )
        legend_box = draw.textbbox((0, 0), legend_label, font=small_font)
        legend_x += 58 + legend_box[2] - legend_box[0]


def render_fear_dca_chart(
    *,
    vix_closes: pd.Series[float],
    vxn_closes: pd.Series[float],
    vix_metrics: FearMetrics,
    vxn_metrics: FearMetrics,
    spy_decision: RecommendationDecision,
    qqqm_decision: RecommendationDecision,
) -> bytes:
    """Render the latest 60 aligned sessions after 119-session MA60 warm-up.

    A shorter history may still produce a valid recommendation, but it cannot
    produce this chart and should use the existing text-only degradation path.
    """
    vix = _validated_series("VIX", vix_closes)
    vxn = _validated_series("VXN", vxn_closes)
    if not vix.tail(_SESSIONS).index.equals(vxn.tail(_SESSIONS).index):
        raise ValueError("VIX and VXN latest 60 sessions must be aligned")
    _validate_annotations("VIX", vix, vix_metrics, spy_decision)
    _validate_annotations("VXN", vxn, vxn_metrics, qqqm_decision)

    image = Image.new("RGB", (CHART_WIDTH, CHART_HEIGHT), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = _font(27, bold=True)
    panel_title_font = _font(23, bold=True)
    body_font = _font(17)
    small_font = _font(15)
    draw.text(
        (40, 24),
        "恐慌指数｜最近60个已完成交易日",
        fill=_TEXT,
        font=title_font,
    )
    draw.text(
        (CHART_WIDTH - 365, 31),
        "Close / MA20 / MA60 / trigger threshold",
        fill=_MUTED,
        font=small_font,
    )
    _draw_panel(
        draw,
        panel_top=75,
        label="VIX",
        closes=vix,
        threshold=25.0,
        decision=spy_decision,
        title_font=panel_title_font,
        body_font=body_font,
        small_font=small_font,
    )
    _draw_panel(
        draw,
        panel_top=485,
        label="VXN",
        closes=vxn,
        threshold=35.0,
        decision=qqqm_decision,
        title_font=panel_title_font,
        body_font=body_font,
        small_font=small_font,
    )
    draw.text(
        (40, 915),
        "Thresholds indicate the first fear-buy tier; moving averages are context only.",
        fill=_MUTED,
        font=small_font,
    )

    output = BytesIO()
    image.save(output, format="PNG", compress_level=9, optimize=False)
    return output.getvalue()
