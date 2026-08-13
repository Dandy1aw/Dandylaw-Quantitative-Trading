"""Daily fear-index DCA recommendation pipeline and persisted-card replay."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4, uuid5

import pandas as pd
import structlog

from quant_signal.calendar import trading_sessions_ending
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.fear_dca import (
    calculate_etf_metrics,
    calculate_fear_metrics,
    recommend_qqqm,
    recommend_spy,
)
from quant_signal.fear_dca_chart import render_fear_dca_chart
from quant_signal.notifier.base import Card, IdempotentCardNotifier
from quant_signal.notifier.cards import fear_dca_card, fear_dca_incomplete_card
from quant_signal.notifier.feishu import ImageUploader
from quant_signal.pipelines.us_briefing import last_completed_us_session

if TYPE_CHECKING:
    from quant_signal.engine import Engine


log = structlog.get_logger()
_MESSAGE_NAMESPACE = UUID("d107b11c-2cce-4f0e-aed9-f156e98cf4aa")


def _delivery_now() -> datetime:
    """Return a fresh clock reading for short-lived delivery leases."""
    return datetime.now(UTC)


def _message_uuid(session: date, status: str) -> str:
    return str(uuid5(_MESSAGE_NAMESPACE, f"fear_dca:{session.isoformat()}:{status}"))


def _supports_provider_idempotency(engine: Engine) -> bool:
    notifier = engine.notifier
    return (
        isinstance(notifier, IdempotentCardNotifier)
        and notifier.supports_message_uuid
    )


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
    expected_send_status: str | None = None,
    expected_claim_token: str | None = None,
) -> bool:
    try:
        delivered = engine.notifier.send(card)
        send_error = None if delivered else "notifier rejected fear DCA card"
    except Exception as error:  # noqa: BLE001 - notifier boundary is persisted
        send_error = _error_text(error)
        log.warning("fear_dca.send_failed", error=send_error)
        updated = engine.ledger.update_fear_dca_delivery(
            session,
            expected_status=expected_status,
            expected_send_status=expected_send_status,
            expected_claim_token=expected_claim_token,
            send_error=send_error,
        )
        if not updated:
            log.info(
                "fear_dca.stale_delivery_update_skipped",
                session=session.isoformat(),
                expected_status=expected_status,
            )
        return False
    updated = engine.ledger.update_fear_dca_delivery(
        session,
        expected_status=expected_status,
        expected_send_status=expected_send_status,
        expected_claim_token=expected_claim_token,
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


def _claim_and_send(
    engine: Engine,
    session: date,
    card: Card,
    *,
    expected_status: str,
    now: datetime,
    initial_owner: bool = False,
) -> bool:
    supports_idempotency = _supports_provider_idempotency(engine)
    claim_token = engine.ledger.claim_fear_dca_delivery(
        session,
        expected_status=expected_status,
        now=now,
        initial_owner=initial_owner,
        allow_retry=supports_idempotency,
        allow_expired_reclaim=supports_idempotency,
    )
    if claim_token is None:
        log.info(
            "fear_dca.delivery_deferred",
            session=session.isoformat(),
            status=expected_status,
            actionable=(
                "configure an idempotent app notifier or recover manually"
                if not supports_idempotency
                else "delivery lease is active or outside provider dedupe window"
            ),
        )
        return False
    return _send_and_record(
        engine,
        session,
        card,
        expected_status=expected_status,
        expected_send_status="IN_FLIGHT",
        expected_claim_token=claim_token,
    )


def _fail_closed(
    engine: Engine,
    target_session: date,
    now: datetime,
    error: BaseException,
    *,
    source_label: str,
) -> bool:
    error_text = _error_text(error)
    created = engine.ledger.save_failed_fear_dca_run(
        target_session,
        source=source_label,
        error=error_text,
        chart_status="PENDING",
        send_status="PENDING",
        now=now,
    )
    supports_idempotency = _supports_provider_idempotency(engine)
    claim_token = engine.ledger.claim_failed_fear_dca_delivery(
        target_session,
        now=_delivery_now(),
        initial_owner=created,
        allow_retry=supports_idempotency,
        allow_expired_reclaim=supports_idempotency,
    )
    if claim_token is None:
        log.info("fear_dca.incomplete_duplicate", session=target_session.isoformat())
        return False
    card = fear_dca_incomplete_card(
        target_session=target_session,
        error=error_text,
    )
    card = dataclasses.replace(
        card,
        message_uuid=_message_uuid(target_session, "FAILED"),
    )
    _send_and_record(
        engine,
        target_session,
        card,
        expected_status="FAILED",
        expected_send_status="IN_FLIGHT",
        expected_claim_token=claim_token,
    )
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


def _aligned_chart_closes(
    vix_closes: pd.Series[float],
    vxn_closes: pd.Series[float],
) -> tuple[pd.Series[float], pd.Series[float]]:
    """Align chart-only histories without changing recommendation inputs."""
    aligned = pd.concat(
        {"vix": vix_closes, "vxn": vxn_closes},
        axis=1,
        join="inner",
    ).sort_index()
    return (
        cast("pd.Series[float]", aligned["vix"]),
        cast("pd.Series[float]", aligned["vxn"]),
    )


def _exact_session_closes(
    closes: pd.Series[float],
    *,
    symbol: str,
    target_session: date,
    sessions: int,
) -> pd.Series[float]:
    """Select the required NYSE window and reject any missing expected bar."""
    expected_dates = trading_sessions_ending(target_session, sessions)
    expected_index = pd.DatetimeIndex(
        [pd.Timestamp(session, tz="UTC") for session in expected_dates],
        name=closes.index.name,
    )
    missing = expected_index.difference(pd.DatetimeIndex(closes.index))
    if not missing.empty:
        missing_dates = ", ".join(stamp.date().isoformat() for stamp in missing)
        raise ValueError(f"{symbol} is missing expected NYSE sessions: {missing_dates}")
    return closes.loc[expected_index]


def run(
    engine: Engine,
    now: datetime,
    source: _DailySource | None = None,
) -> bool:
    """Calculate, persist, and deliver one recommendation for the target session."""
    if now.tzinfo is None:
        raise ValueError("fear DCA run time must be timezone-aware")
    target_session = last_completed_us_session(now)
    settings = engine.settings.fear_dca
    symbols = [
        settings.vix_symbol,
        settings.vxn_symbol,
        settings.spy_symbol,
        settings.qqqm_symbol,
    ]
    existing = engine.ledger.fear_dca_run(target_session)
    if existing is not None and existing["status"] == "COMPLETE":
        if existing["send_status"] == "SENT":
            log.info("fear_dca.complete_skip", session=target_session.isoformat())
            return True
        existing_card = existing.get("card")
        if not isinstance(existing_card, Card):
            log.warning(
                "fear_dca.complete_delivery_blocked",
                session=target_session.isoformat(),
                reason="persisted card is missing",
            )
            return False
        return _claim_and_send(
            engine,
            target_session,
            existing_card,
            expected_status="COMPLETE",
            now=_delivery_now(),
        )
    if (
        existing is not None
        and existing["status"] == "FAILED"
        and existing["send_status"] in ("PENDING", "IN_FLIGHT")
    ):
        claim_token = engine.ledger.claim_failed_fear_dca_delivery(
            target_session,
            now=_delivery_now(),
            initial_owner=False,
            allow_retry=_supports_provider_idempotency(engine),
            allow_expired_reclaim=_supports_provider_idempotency(engine),
        )
        if claim_token is None:
            log.info(
                "fear_dca.failed_delivery_deferred",
                session=target_session.isoformat(),
                actionable=(
                    "configure an idempotent app notifier to recover expired delivery"
                    if not _supports_provider_idempotency(engine)
                    else "delivery lease is still active"
                ),
            )
            return False
        alert = fear_dca_incomplete_card(
            target_session=target_session,
            error=str(existing.get("error") or "unknown data error"),
        )
        alert = dataclasses.replace(
            alert,
            message_uuid=_message_uuid(target_session, "FAILED"),
        )
        _send_and_record(
            engine,
            target_session,
            alert,
            expected_status="FAILED",
            expected_send_status="IN_FLIGHT",
            expected_claim_token=claim_token,
        )
        return False
    daily_source = source if source is not None else YFinanceSource()
    try:
        bars = daily_source.fetch_daily_bars(
            symbols,
            target_session - timedelta(days=settings.lookback_calendar_days),
            target_session + timedelta(days=1),
        )
        closes = {
            symbol: _closes_for_symbol(bars, symbol, target_session)
            for symbol in symbols
        }
        vix_metrics = calculate_fear_metrics(
            _exact_session_closes(
                closes[settings.vix_symbol],
                symbol=settings.vix_symbol,
                target_session=target_session,
                sessions=60,
            )
        )
        vxn_metrics = calculate_fear_metrics(
            _exact_session_closes(
                closes[settings.vxn_symbol],
                symbol=settings.vxn_symbol,
                target_session=target_session,
                sessions=60,
            )
        )
        spy_metrics = calculate_etf_metrics(
            _exact_session_closes(
                closes[settings.spy_symbol],
                symbol=settings.spy_symbol,
                target_session=target_session,
                sessions=21,
            )
        )
        qqqm_metrics = calculate_etf_metrics(
            _exact_session_closes(
                closes[settings.qqqm_symbol],
                symbol=settings.qqqm_symbol,
                target_session=target_session,
                sessions=21,
            )
        )
        spy_decision = recommend_spy(vix_metrics, spy_metrics)
        qqqm_decision = recommend_qqqm(vxn_metrics, qqqm_metrics)
    except Exception as error:  # noqa: BLE001 - all input failures fail closed
        log.warning(
            "fear_dca.data_incomplete",
            session=target_session.isoformat(),
            error=_error_text(error),
        )
        return _fail_closed(
            engine,
            target_session,
            now,
            error,
            source_label=settings.source_label,
        )

    image_key: str | None = None
    chart_status = "DEGRADED"
    chart_error: str | None = None
    try:
        chart_vix_closes, chart_vxn_closes = _aligned_chart_closes(
            closes[settings.vix_symbol],
            closes[settings.vxn_symbol],
        )
        image_bytes = render_fear_dca_chart(
            vix_closes=chart_vix_closes,
            vxn_closes=chart_vxn_closes,
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
        source_label=settings.source_label,
    )
    card = dataclasses.replace(
        card,
        message_uuid=_message_uuid(target_session, "COMPLETE"),
    )
    created = engine.ledger.save_complete_fear_dca_run(
        target_session,
        source=settings.source_label,
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
        current = engine.ledger.fear_dca_run(target_session)
        if current is not None and current["status"] == "COMPLETE":
            current_card = current.get("card")
            if current["send_status"] == "SENT":
                log.info(
                    "fear_dca.complete_race_skip",
                    session=target_session.isoformat(),
                )
                return True
            if isinstance(current_card, Card):
                return _claim_and_send(
                    engine,
                    target_session,
                    current_card,
                    expected_status="COMPLETE",
                    now=_delivery_now(),
                )
        log.info(
            "fear_dca.complete_deferred",
            session=target_session.isoformat(),
            reason="incomplete_notice_in_flight",
        )
        return False
    return _claim_and_send(
        engine,
        target_session,
        card,
        expected_status="COMPLETE",
        now=_delivery_now(),
        initial_owner=True,
    )


def retry_delivery(engine: Engine, now: datetime) -> bool:
    """Retry the latest unsent COMPLETE card without refetching market data."""
    if now.tzinfo is None:
        raise ValueError("fear DCA retry time must be timezone-aware")
    run_record = engine.ledger.latest_unsent_complete_fear_dca_run()
    if run_record is None:
        log.info("fear_dca.retry_skip", reason="no_unsent_complete_card")
        return False
    card = run_record.get("card")
    if not isinstance(card, Card):
        log.warning("fear_dca.retry_blocked", reason="persisted card is missing")
        return False
    return _claim_and_send(
        engine,
        date.fromisoformat(str(run_record["session_date"])),
        card,
        expected_status="COMPLETE",
        now=now,
    )


def replay(engine: Engine) -> bool:
    """Resend the latest persisted complete card without fetching market data."""
    card = engine.ledger.latest_complete_fear_dca_card()
    if card is None:
        log.info("fear_dca.replay_skip", reason="no_complete_card")
        return False
    replay_card = dataclasses.replace(card, message_uuid=str(uuid4()))
    try:
        return engine.notifier.send(replay_card)
    except Exception as error:  # noqa: BLE001 - replay reports delivery failure
        log.warning("fear_dca.replay_failed", error=_error_text(error))
        return False
