"""Daily fear-index DCA recommendation pipeline and persisted-card replay."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast

import pandas as pd
import structlog

from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.fear_dca import (
    calculate_etf_metrics,
    calculate_fear_metrics,
    recommend_qqqm,
    recommend_spy,
)
from quant_signal.fear_dca_chart import render_fear_dca_chart
from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import fear_dca_card, fear_dca_incomplete_card
from quant_signal.notifier.feishu import ImageUploader
from quant_signal.pipelines.us_briefing import last_completed_us_session

if TYPE_CHECKING:
    from quant_signal.engine import Engine


log = structlog.get_logger()
SYMBOLS = ["^VIX", "^VXN", "SPY", "QQQM"]


class _DailySource(Protocol):
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame: ...


def _error_text(error: BaseException) -> str:
    return str(error).strip() or type(error).__name__


def _send_and_record(
    engine: Engine,
    session: date,
    card: Card,
    *,
    expected_status: str,
) -> bool:
    try:
        delivered = engine.notifier.send(card)
        send_error = None if delivered else "notifier rejected fear DCA card"
    except Exception as error:  # noqa: BLE001 - notifier boundary is persisted
        delivered = False
        send_error = _error_text(error)
        log.warning("fear_dca.send_failed", error=send_error)
    updated = engine.ledger.update_fear_dca_delivery(
        session,
        expected_status=expected_status,
        send_status="SENT" if delivered else "FAILED",
        send_error=send_error,
    )
    if not updated:
        log.info(
            "fear_dca.stale_delivery_update_skipped",
            session=session.isoformat(),
            expected_status=expected_status,
        )
    return delivered


def _fail_closed(
    engine: Engine, target_session: date, now: datetime, error: BaseException
) -> bool:
    error_text = _error_text(error)
    claimed = engine.ledger.save_failed_fear_dca_run(
        target_session,
        source="yfinance",
        error=error_text,
        chart_status="PENDING",
        send_status="PENDING",
        now=now,
    )
    if claimed:
        current = engine.ledger.fear_dca_run(target_session)
        if current is None or current["status"] != "FAILED":
            log.info(
                "fear_dca.stale_incomplete_notice_skipped",
                session=target_session.isoformat(),
            )
            return False
        card = fear_dca_incomplete_card(
            target_session=target_session,
            error=error_text,
        )
        _send_and_record(
            engine,
            target_session,
            card,
            expected_status="FAILED",
        )
    else:
        log.info("fear_dca.incomplete_duplicate", session=target_session.isoformat())
    return False


def _plain_mapping(**values: object) -> dict[str, object]:
    def plain(value: object) -> object:
        if dataclasses.is_dataclass(value):
            return plain(dataclasses.asdict(value))  # type: ignore[arg-type]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    return {key: plain(value) for key, value in values.items()}


def _closes_for_symbol(
    bars: pd.DataFrame, symbol: str, target_session: date
) -> pd.Series[float]:
    if not isinstance(bars.index, pd.MultiIndex):
        raise TypeError("daily bars must use a ticker/timestamp MultiIndex")
    try:
        symbol_bars = bars.xs(symbol, level="ticker")
    except (KeyError, ValueError) as error:
        raise ValueError(f"{symbol} data is missing") from error
    if "close" not in symbol_bars.columns:
        raise ValueError(f"{symbol} close data is missing")

    timestamps = pd.to_datetime(symbol_bars.index, utc=True)
    session_dates = [stamp.date() for stamp in timestamps]
    normalized = pd.Series(
        pd.to_numeric(symbol_bars["close"], errors="coerce").to_numpy(dtype=float),
        index=pd.DatetimeIndex(
            [pd.Timestamp(session, tz="UTC") for session in session_dates],
            name="ts",
        ),
        name="close",
        dtype=float,
    )
    target_timestamp = pd.Timestamp(target_session, tz="UTC")
    normalized = normalized.loc[normalized.index <= target_timestamp].sort_index()
    if not normalized.index.is_unique:
        raise ValueError(f"{symbol} contains duplicate sessions")
    if target_timestamp not in normalized.index:
        raise ValueError(f"{symbol} is missing target session {target_session.isoformat()}")
    if normalized.index[-1] != target_timestamp:
        raise ValueError(f"{symbol} target session is not the latest completed bar")
    return cast("pd.Series[float]", normalized)


def run(
    engine: Engine,
    now: datetime,
    source: _DailySource | None = None,
) -> bool:
    """Calculate, persist, and deliver one recommendation for the target session."""
    if now.tzinfo is None:
        raise ValueError("fear DCA run time must be timezone-aware")
    target_session = last_completed_us_session(now)
    existing = engine.ledger.fear_dca_run(target_session)
    if existing is not None and existing["status"] == "COMPLETE":
        log.info("fear_dca.complete_skip", session=target_session.isoformat())
        return True
    daily_source = source if source is not None else YFinanceSource()
    try:
        bars = daily_source.fetch_daily_bars(
            SYMBOLS,
            target_session - timedelta(days=220),
            target_session + timedelta(days=1),
        )
        closes = {
            symbol: _closes_for_symbol(bars, symbol, target_session)
            for symbol in SYMBOLS
        }
        vix_metrics = calculate_fear_metrics(closes["^VIX"])
        vxn_metrics = calculate_fear_metrics(closes["^VXN"])
        spy_metrics = calculate_etf_metrics(closes["SPY"])
        qqqm_metrics = calculate_etf_metrics(closes["QQQM"])
        spy_decision = recommend_spy(vix_metrics, spy_metrics)
        qqqm_decision = recommend_qqqm(vxn_metrics, qqqm_metrics)
    except Exception as error:  # noqa: BLE001 - all input failures fail closed
        log.warning(
            "fear_dca.data_incomplete",
            session=target_session.isoformat(),
            error=_error_text(error),
        )
        return _fail_closed(engine, target_session, now, error)

    image_key: str | None = None
    chart_status = "DEGRADED"
    chart_error: str | None = None
    try:
        image_bytes = render_fear_dca_chart(
            vix_closes=closes["^VIX"],
            vxn_closes=closes["^VXN"],
            vix_metrics=vix_metrics,
            vxn_metrics=vxn_metrics,
            spy_decision=spy_decision,
            qqqm_decision=qqqm_decision,
        )
        if isinstance(engine.notifier, ImageUploader):
            image_key = engine.notifier.upload_image(image_bytes)
            if not image_key:
                raise ValueError("image upload returned an empty key")
            chart_status = "UPLOADED"
        else:
            chart_error = "notifier does not support image upload"
    except Exception as error:  # noqa: BLE001 - chart failure uses text fallback
        chart_error = str(error) or type(error).__name__
        log.warning("fear_dca.chart_degraded", error=chart_error)

    card = fear_dca_card(
        target_session=target_session,
        generated_at=now,
        vix_metrics=vix_metrics,
        vxn_metrics=vxn_metrics,
        spy_metrics=spy_metrics,
        qqqm_metrics=qqqm_metrics,
        spy_decision=spy_decision,
        qqqm_decision=qqqm_decision,
        image_key=image_key,
    )
    created = engine.ledger.save_complete_fear_dca_run(
        target_session,
        source="yfinance",
        metrics=_plain_mapping(
            vix=vix_metrics,
            vxn=vxn_metrics,
            spy=spy_metrics,
            qqqm=qqqm_metrics,
        ),
        decisions=_plain_mapping(spy=spy_decision, qqqm=qqqm_decision),
        card=card,
        chart_status=chart_status,
        send_status="PENDING",
        chart_error=chart_error,
        now=now,
    )
    if not created:
        log.info("fear_dca.complete_race_skip", session=target_session.isoformat())
        return True
    return _send_and_record(
        engine,
        target_session,
        card,
        expected_status="COMPLETE",
    )


def replay(engine: Engine) -> bool:
    """Resend the latest persisted complete card without fetching market data."""
    card = engine.ledger.latest_complete_fear_dca_card()
    if card is None:
        log.info("fear_dca.replay_skip", reason="no_complete_card")
        return False
    try:
        return engine.notifier.send(card)
    except Exception as error:  # noqa: BLE001 - replay reports delivery failure
        log.warning("fear_dca.replay_failed", error=_error_text(error))
        return False
