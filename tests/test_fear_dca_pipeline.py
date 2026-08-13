from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from conftest import make_test_settings

from quant_signal.calendar import trading_sessions_ending
from quant_signal.config import FearDcaSettings
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card, CardKind
from quant_signal.pipelines import fear_dca as pipeline
from quant_signal.pipelines.fear_dca import replay, retry_delivery, run

SYMBOLS = ["^VIX", "^VXN", "SPY", "QQQM"]
ASIA = ZoneInfo("Asia/Shanghai")
TARGET = date(2026, 8, 14)


class FakeSource:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars
        self.calls: list[tuple[list[str], date, date]] = []

    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        self.calls.append((tickers, start, end))
        return self.bars


class FakeNotifier:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.cards: list[Card] = []
        self.succeeds = succeeds

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return self.succeeds


class FakeImageNotifier(FakeNotifier):
    def __init__(
        self, *, image_key: str = "img_v2_fear", upload_error: Exception | None = None
    ) -> None:
        super().__init__()
        self.image_key = image_key
        self.upload_error = upload_error
        self.uploaded: list[bytes] = []

    def upload_image(self, image_bytes: bytes) -> str:
        self.uploaded.append(image_bytes)
        if self.upload_error is not None:
            raise self.upload_error
        return self.image_key


def _bars(
    *,
    periods: int = 119,
    target: date = TARGET,
    starts: dict[str, float] | None = None,
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=target, periods=periods, tz="UTC")
    frames: list[pd.DataFrame] = []
    symbol_starts = starts or {
        "^VIX": 20.0,
        "^VXN": 30.0,
        "SPY": 500.0,
        "QQQM": 200.0,
    }
    for symbol, starting_close in symbol_starts.items():
        closes = np.linspace(starting_close, starting_close * 1.2, periods)
        frame = pd.DataFrame(
            {
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": np.full(periods, 1_000_000.0),
            },
            index=pd.MultiIndex.from_product(
                [[symbol], sessions], names=["ticker", "ts"]
            ),
        )
        frames.append(frame)
    return pd.concat(frames).sort_index()


def _limit_symbol(bars: pd.DataFrame, symbol: str, periods: int) -> pd.DataFrame:
    selected = bars.xs(symbol, level="ticker").tail(periods)
    selected.index = pd.MultiIndex.from_product(
        [[symbol], selected.index], names=["ticker", "ts"]
    )
    others = bars.drop(index=symbol, level="ticker")
    return pd.concat([others, selected]).sort_index()


def _limit_symbol_to_sessions(
    bars: pd.DataFrame, symbol: str, sessions: tuple[date, ...]
) -> pd.DataFrame:
    selected = bars.xs(symbol, level="ticker")
    wanted = pd.DatetimeIndex([pd.Timestamp(session, tz="UTC") for session in sessions])
    selected = selected.loc[wanted]
    selected.index = pd.MultiIndex.from_product(
        [[symbol], selected.index], names=["ticker", "ts"]
    )
    others = bars.drop(index=symbol, level="ticker")
    return pd.concat([others, selected]).sort_index()


def _drop_session(bars: pd.DataFrame, symbol: str, session: date) -> pd.DataFrame:
    timestamp = pd.Timestamp(session, tz="UTC")
    return bars.drop(index=(symbol, timestamp))


def _set_close(
    bars: pd.DataFrame, symbol: str, offset: int, value: float
) -> pd.DataFrame:
    changed = bars.copy()
    symbol_index = changed.xs(symbol, level="ticker").index
    changed.loc[(symbol, symbol_index[offset]), "close"] = value
    return changed


def _set_session_close(
    bars: pd.DataFrame, symbol: str, session: date, value: float
) -> pd.DataFrame:
    changed = bars.copy()
    changed.loc[(symbol, pd.Timestamp(session, tz="UTC")), "close"] = value
    return changed


def _add_symbol_session(
    bars: pd.DataFrame,
    symbol: str,
    session: date,
) -> pd.DataFrame:
    symbol_bars = bars.xs(symbol, level="ticker")
    row = symbol_bars.iloc[[-1]].copy()
    row.index = pd.MultiIndex.from_tuples(
        [(symbol, pd.Timestamp(session, tz="UTC"))],
        names=["ticker", "ts"],
    )
    return pd.concat([bars, row]).sort_index()


def _engine(
    tmp_path: Path,
    notifier: FakeNotifier,
    *,
    fear_dca: FearDcaSettings | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        ledger=SignalLedger(tmp_path / "signals.db"),
        notifier=notifier,
        settings=make_test_settings(fear_dca=fear_dca or FearDcaSettings()),
        source=SimpleNamespace(
            fetch_daily_bars=lambda *_args: (_ for _ in ()).throw(
                AssertionError("engine.source must not be used")
            )
        ),
    )


def test_monday_asia_run_uses_prior_friday_and_220_day_fetch_window(
    tmp_path: Path,
) -> None:
    source = FakeSource(_bars())
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert run(engine, now, source=source)

    assert source.calls == [
        (SYMBOLS, TARGET - timedelta(days=220), TARGET + timedelta(days=1))
    ]
    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert len(notifier.cards) == 1


def test_pipeline_uses_configured_symbols_lookback_and_source_label(
    tmp_path: Path,
) -> None:
    configured_starts = {
        "VIX-CFG": 20.0,
        "VXN-CFG": 30.0,
        "SPY-CFG": 500.0,
        "QQQM-CFG": 200.0,
    }
    source = FakeSource(_bars(starts=configured_starts))
    notifier = FakeNotifier()
    config = FearDcaSettings(
        vix_symbol="VIX-CFG",
        vxn_symbol="VXN-CFG",
        spy_symbol="SPY-CFG",
        qqqm_symbol="QQQM-CFG",
        lookback_calendar_days=180,
        source_label="Configured daily source",
    )
    engine = _engine(tmp_path, notifier, fear_dca=config)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert run(engine, now, source=source)

    assert source.calls == [
        (
            ["VIX-CFG", "VXN-CFG", "SPY-CFG", "QQQM-CFG"],
            TARGET - timedelta(days=180),
            TARGET + timedelta(days=1),
        )
    ]
    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["source"] == "Configured daily source"
    assert "Configured daily source" in notifier.cards[0].body_md


def test_asia_run_after_us_holiday_uses_prior_open_session(tmp_path: Path) -> None:
    target = date(2026, 9, 4)  # Friday before US Labor Day.
    source = FakeSource(_bars(target=target))
    notifier = FakeNotifier()

    assert run(
        _engine(tmp_path, notifier),
        datetime(2026, 9, 8, 9, 30, tzinfo=ASIA),
        source=source,
    )

    assert source.calls[0][1:] == (
        target - timedelta(days=220),
        target + timedelta(days=1),
    )


def test_default_source_is_dedicated_yfinance_not_engine_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource(_bars())
    monkeypatch.setattr(pipeline, "YFinanceSource", lambda: source)

    assert run(
        _engine(tmp_path, FakeNotifier()),
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
    )
    assert len(source.calls) == 1


def test_missing_exact_target_session_fails_closed_and_notifies_once(
    tmp_path: Path,
) -> None:
    source = FakeSource(_drop_session(_bars(), "QQQM", TARGET))
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert not run(engine, now, source=source)
    assert not run(engine, now + timedelta(minutes=1), source=source)

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "FAILED"
    assert "QQQM" in str(stored["error"])
    assert stored["send_status"] == "SENT"
    assert len(notifier.cards) == 1
    assert notifier.cards[0].kind is CardKind.ALERT
    assert "暂停" in notifier.cards[0].body_md


@pytest.mark.parametrize(
    ("symbol", "periods"),
    [("^VIX", 59), ("SPY", 20)],
    ids=["fear-needs-60", "etf-needs-21"],
)
def test_recommendation_windows_fail_closed(
    tmp_path: Path, symbol: str, periods: int
) -> None:
    source = FakeSource(_limit_symbol(_bars(), symbol, periods))
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)

    assert not run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=source,
    )
    assert engine.ledger.fear_dca_run(TARGET)["status"] == "FAILED"  # type: ignore[index]


@pytest.mark.parametrize(
    ("symbol", "required_sessions"),
    [("^VIX", 60), ("^VXN", 60), ("SPY", 21), ("QQQM", 21)],
)
def test_missing_middle_expected_nyse_session_fails_closed(
    tmp_path: Path, symbol: str, required_sessions: int
) -> None:
    missing = trading_sessions_ending(TARGET, required_sessions)[-10]
    engine = _engine(tmp_path, FakeNotifier())

    assert not run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(_drop_session(_bars(), symbol, missing)),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "FAILED"
    assert symbol in str(stored["error"])
    assert missing.isoformat() in str(stored["error"])


def test_extra_non_nyse_dates_do_not_shift_exact_etf_return_offsets(
    tmp_path: Path,
) -> None:
    bars = _bars()
    sessions = trading_sessions_ending(TARGET, 21)
    for offset, session in enumerate(sessions):
        bars = _set_session_close(bars, "SPY", session, 100.0 + offset)
        bars = _set_session_close(bars, "QQQM", session, 200.0 + offset)
    extra = date(2026, 8, 8)  # Saturday inside the return window.
    bars = _set_session_close(
        _add_symbol_session(bars, "SPY", extra), "SPY", extra, 9_999.0
    )
    bars = _set_session_close(
        _add_symbol_session(bars, "QQQM", extra), "QQQM", extra, 8_888.0
    )
    engine = _engine(tmp_path, FakeNotifier())

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(bars),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    metrics = stored["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["spy"]["one_session_return"] == pytest.approx(120 / 119 - 1)
    assert metrics["spy"]["five_session_return"] == pytest.approx(120 / 115 - 1)
    assert metrics["spy"]["twenty_session_return"] == pytest.approx(120 / 100 - 1)
    assert metrics["qqqm"]["one_session_return"] == pytest.approx(220 / 219 - 1)
    assert metrics["qqqm"]["five_session_return"] == pytest.approx(220 / 215 - 1)
    assert metrics["qqqm"]["twenty_session_return"] == pytest.approx(220 / 200 - 1)


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, 0.0])
def test_nonfinite_or_nonpositive_recommendation_input_fails_closed(
    tmp_path: Path, bad_value: float
) -> None:
    source = FakeSource(_set_close(_bars(), "^VIX", -2, bad_value))
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)

    assert not run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=source,
    )
    assert len(notifier.cards) == 1
    assert "finite positive" in str(engine.ledger.fear_dca_run(TARGET)["error"])  # type: ignore[index]


def test_fixed_data_supersedes_failed_run_and_sends_recommendation(
    tmp_path: Path,
) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert not run(
        engine,
        now,
        source=FakeSource(_drop_session(_bars(), "QQQM", TARGET)),
    )
    assert run(engine, now + timedelta(minutes=2), source=FakeSource(_bars()))

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["error"] is None
    assert len(notifier.cards) == 2
    assert notifier.cards[-1].kind is CardKind.REPORT


def test_late_failed_alert_delivery_cannot_corrupt_recovered_complete_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "render_fear_dca_chart", lambda **_kwargs: b"png")
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    valid_source = FakeSource(_bars())

    class InterleavingNotifier:
        def __init__(self) -> None:
            self.engine: SimpleNamespace | None = None
            self.cards: list[Card] = []
            self.recovered = False

        def send(self, card: Card) -> bool:
            self.cards.append(card)
            if card.kind is CardKind.ALERT and not self.recovered:
                self.recovered = True
                assert self.engine is not None
                assert not run(
                    self.engine,
                    now + timedelta(minutes=1),
                    source=valid_source,
                )
                return True
            return False

    notifier = InterleavingNotifier()
    engine = SimpleNamespace(
        ledger=SignalLedger(tmp_path / "signals.db"),
        notifier=notifier,
        settings=make_test_settings(fear_dca=FearDcaSettings()),
        source=SimpleNamespace(),
    )
    notifier.engine = engine

    assert not run(
        engine,
        now,
        source=FakeSource(_drop_session(_bars(), "QQQM", TARGET)),
    )

    in_flight_recovery = engine.ledger.fear_dca_run(TARGET)
    assert in_flight_recovery is not None
    assert in_flight_recovery["status"] == "FAILED"
    assert in_flight_recovery["send_status"] == "SENT"
    assert [card.kind for card in notifier.cards] == [CardKind.ALERT]

    assert not run(
        engine,
        now + timedelta(minutes=1, seconds=1),
        source=valid_source,
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["send_status"] == "FAILED"
    assert stored["send_error"] == "notifier rejected fear DCA card"
    assert isinstance(stored["card"], Card)
    assert stored["card"].kind is CardKind.REPORT
    assert [card.kind for card in notifier.cards] == [CardKind.ALERT, CardKind.REPORT]


def test_failed_run_reclaims_expired_crashed_notice_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class IdempotentFakeNotifier(FakeNotifier):
        supports_message_uuid = True

    notifier = IdempotentFakeNotifier()
    engine = _engine(tmp_path, notifier)
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert engine.ledger.save_failed_fear_dca_run(
        TARGET,
        source="yfinance",
        error="crashed sender",
        now=scheduled,
    )
    assert engine.ledger.claim_failed_fear_dca_delivery(
        TARGET, now=delivery_start
    )

    monkeypatch.setattr(
        pipeline,
        "_delivery_now",
        lambda: delivery_start + timedelta(minutes=11),
    )
    bad_source = FakeSource(_drop_session(_bars(), "QQQM", TARGET))
    assert not run(engine, scheduled + timedelta(minutes=1), source=bad_source)
    assert len(notifier.cards) == 1
    assert notifier.cards[0].kind is CardKind.ALERT
    assert not run(engine, scheduled + timedelta(minutes=2), source=bad_source)
    assert len(notifier.cards) == 1


def test_live_failed_delivery_lease_suppresses_second_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert engine.ledger.save_failed_fear_dca_run(
        TARGET,
        source="yfinance",
        error="crashed sender",
        now=scheduled,
    )
    assert engine.ledger.claim_failed_fear_dca_delivery(
        TARGET, now=delivery_start
    )
    monkeypatch.setattr(
        pipeline,
        "_delivery_now",
        lambda: delivery_start + timedelta(minutes=1),
    )

    assert not run(
        engine,
        scheduled + timedelta(minutes=10),
        source=FakeSource(_drop_session(_bars(), "QQQM", TARGET)),
    )
    assert notifier.cards == []


def test_complete_recovery_uses_fresh_delivery_clock_not_scheduled_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert engine.ledger.save_failed_fear_dca_run(
        TARGET,
        source="yfinance",
        error="sending now",
        now=scheduled,
    )
    assert engine.ledger.claim_failed_fear_dca_delivery(
        TARGET, now=delivery_start
    )
    monkeypatch.setattr(
        pipeline,
        "_delivery_now",
        lambda: delivery_start + timedelta(minutes=1),
    )

    assert not run(
        engine,
        scheduled + timedelta(minutes=10),
        source=FakeSource(_bars()),
    )
    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "FAILED"
    assert notifier.cards == []


def test_complete_session_skips_fetch_and_duplicate_send(tmp_path: Path) -> None:
    source = FakeSource(_bars())
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert run(engine, now, source=source)
    assert run(engine, now + timedelta(minutes=1), source=source)

    assert len(source.calls) == 1
    assert len(notifier.cards) == 1


def test_60_session_recommendation_completes_with_degraded_text_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = trading_sessions_ending(TARGET, 60)
    bars = _limit_symbol_to_sessions(_bars(), "^VIX", expected)
    bars = _limit_symbol_to_sessions(bars, "^VXN", expected)
    notifier = FakeImageNotifier()
    engine = _engine(tmp_path, notifier)

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(bars),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["chart_status"] == "DEGRADED"
    assert "119 sessions" in str(stored["chart_error"])
    assert notifier.cards[0].image_key is None


def test_chart_aligns_vix_vxn_on_common_sessions_before_render(
    tmp_path: Path,
) -> None:
    bars = _bars(periods=120)
    bars = _add_symbol_session(bars, "^VIX", date(2026, 8, 8))
    bars = _add_symbol_session(bars, "^VXN", date(2026, 8, 9))
    notifier = FakeImageNotifier()
    engine = _engine(tmp_path, notifier)

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(bars),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["chart_status"] == "UPLOADED"
    assert stored["chart_error"] is None
    assert len(notifier.uploaded) == 1
    assert notifier.cards[0].image_key == "img_v2_fear"


def test_chart_degrades_when_vix_vxn_have_fewer_than_119_common_sessions(
    tmp_path: Path,
) -> None:
    bars = _bars(periods=119)
    first_vxn_session = bars.xs("^VXN", level="ticker").index[0]
    bars = bars.drop(index=("^VXN", first_vxn_session))
    bars = _add_symbol_session(
        bars,
        "^VXN",
        first_vxn_session.date() - timedelta(days=1),
    )
    notifier = FakeImageNotifier()
    engine = _engine(tmp_path, notifier)

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(bars),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["chart_status"] == "DEGRADED"
    assert "119 sessions" in str(stored["chart_error"])
    assert notifier.uploaded == []
    assert notifier.cards[0].image_key is None


def test_chart_upload_sets_image_key_and_persists_canonical_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "render_fear_dca_chart", lambda **_kwargs: b"png")
    notifier = FakeImageNotifier()
    engine = _engine(tmp_path, notifier)

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(_bars()),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert notifier.uploaded == [b"png"]
    assert notifier.cards[0].image_key == "img_v2_fear"
    assert stored["card"] == notifier.cards[0]
    assert stored["chart_status"] == "UPLOADED"
    assert stored["send_status"] == "SENT"
    metrics = stored["metrics"]
    assert isinstance(metrics, dict)
    assert isinstance(metrics["vix"], dict)
    assert metrics["vix"]["interpretation"] == "fear trend confirmed"


@pytest.mark.parametrize("stage", ["render", "upload"])
def test_chart_errors_degrade_to_complete_text_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    error = RuntimeError(f"{stage} broke")
    if stage == "render":
        def broken_render(**_kwargs: object) -> bytes:
            raise error

        monkeypatch.setattr(pipeline, "render_fear_dca_chart", broken_render)
        notifier = FakeImageNotifier()
    else:
        monkeypatch.setattr(
            pipeline, "render_fear_dca_chart", lambda **_kwargs: b"png"
        )
        notifier = FakeImageNotifier(upload_error=error)
    engine = _engine(tmp_path, notifier)

    assert run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(_bars()),
    )

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["chart_status"] == "DEGRADED"
    assert stored["chart_error"] == f"{stage} broke"
    assert notifier.cards[0].image_key is None


def test_send_exception_remains_ambiguous_in_flight_after_complete_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "render_fear_dca_chart", lambda **_kwargs: b"png")
    ledger = SignalLedger(tmp_path / "signals.db")

    class InspectingNotifier:
        def send(self, card: Card) -> bool:
            stored = ledger.fear_dca_run(TARGET)
            assert stored is not None and stored["status"] == "COMPLETE"
            raise RuntimeError("send broke")

    engine = SimpleNamespace(
        ledger=ledger,
        notifier=InspectingNotifier(),
        settings=make_test_settings(fear_dca=FearDcaSettings()),
        source=SimpleNamespace(),
    )

    assert not run(
        engine,
        datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
        source=FakeSource(_bars()),
    )
    stored = ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "COMPLETE"
    assert stored["send_status"] == "IN_FLIGHT"
    assert stored["send_error"] == "send broke"


def test_complete_pending_after_persistence_retries_without_fetch_and_reuses_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class IdempotentNotifier(FakeNotifier):
        supports_message_uuid = True

    notifier = IdempotentNotifier()
    engine = _engine(tmp_path, notifier)
    original_claim = engine.ledger.claim_fear_dca_delivery
    monkeypatch.setattr(
        engine.ledger,
        "claim_fear_dca_delivery",
        lambda *_args, **_kwargs: None,
    )
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert not run(engine, now, source=FakeSource(_bars()))
    pending = engine.ledger.fear_dca_run(TARGET)
    assert pending is not None
    assert pending["send_status"] == "PENDING"
    pending_card = pending["card"]
    assert isinstance(pending_card, Card)
    assert notifier.cards == []

    monkeypatch.setattr(engine.ledger, "claim_fear_dca_delivery", original_claim)
    monkeypatch.setattr(
        pipeline,
        "YFinanceSource",
        lambda: (_ for _ in ()).throw(AssertionError("retry must not fetch data")),
    )
    assert retry_delivery(engine, now + timedelta(minutes=5))

    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["send_status"] == "SENT"
    assert [card.message_uuid for card in notifier.cards] == [
        pending_card.message_uuid
    ]


def test_definitive_complete_delivery_failure_retries_then_becomes_sent(
    tmp_path: Path,
) -> None:
    class IdempotentNotifier(FakeNotifier):
        supports_message_uuid = True

    notifier = IdempotentNotifier(succeeds=False)
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)

    assert not run(engine, now, source=FakeSource(_bars()))
    failed = engine.ledger.fear_dca_run(TARGET)
    assert failed is not None
    assert failed["send_status"] == "FAILED"
    failed_card = failed["card"]
    assert isinstance(failed_card, Card)

    notifier.succeeds = True
    assert retry_delivery(engine, now + timedelta(minutes=5))
    assert [card.message_uuid for card in notifier.cards] == [
        failed_card.message_uuid,
        failed_card.message_uuid,
    ]
    assert engine.ledger.fear_dca_run(TARGET)["send_status"] == "SENT"  # type: ignore[index]


def test_sent_complete_delivery_is_terminal_for_retry(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    assert run(engine, now, source=FakeSource(_bars()))

    assert not retry_delivery(engine, now + timedelta(minutes=5))
    assert len(notifier.cards) == 1


def test_non_idempotent_complete_ambiguous_delivery_is_not_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))

    class AmbiguousNotifier(FakeNotifier):
        def send(self, card: Card) -> bool:
            self.cards.append(card)
            raise KeyboardInterrupt("provider may have accepted")

    notifier = AmbiguousNotifier()
    engine = _engine(tmp_path, notifier)
    monkeypatch.setattr(pipeline, "_delivery_now", lambda: delivery_start)
    with pytest.raises(KeyboardInterrupt):
        run(
            engine,
            datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
            source=FakeSource(_bars()),
        )

    assert not retry_delivery(engine, delivery_start + timedelta(minutes=11))
    assert len(notifier.cards) == 1
    assert engine.ledger.fear_dca_run(TARGET)["send_status"] == "IN_FLIGHT"  # type: ignore[index]


def test_idempotent_complete_ambiguous_delivery_reclaims_with_same_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))

    class CrashOnceNotifier(FakeNotifier):
        supports_message_uuid = True

        def send(self, card: Card) -> bool:
            self.cards.append(card)
            if len(self.cards) == 1:
                raise KeyboardInterrupt("provider may have accepted")
            return True

    notifier = CrashOnceNotifier()
    engine = _engine(tmp_path, notifier)
    monkeypatch.setattr(pipeline, "_delivery_now", lambda: delivery_start)
    with pytest.raises(KeyboardInterrupt):
        run(
            engine,
            datetime(2026, 8, 17, 9, 30, tzinfo=ASIA),
            source=FakeSource(_bars()),
        )

    assert retry_delivery(engine, delivery_start + timedelta(minutes=11))
    assert len(notifier.cards) == 2
    assert notifier.cards[0].message_uuid == notifier.cards[1].message_uuid
    assert engine.ledger.fear_dca_run(TARGET)["send_status"] == "SENT"  # type: ignore[index]


def test_replay_sends_latest_complete_card_without_fetching(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    source = FakeSource(_bars())
    assert run(engine, now, source=source)
    expected = notifier.cards[-1]
    notifier.cards.clear()

    assert replay(engine)
    assert replay(engine)

    assert len(notifier.cards) == 2
    assert notifier.cards[0].message_uuid != expected.message_uuid
    assert notifier.cards[1].message_uuid != notifier.cards[0].message_uuid
    for replayed in notifier.cards:
        assert dataclasses.replace(replayed, message_uuid=expected.message_uuid) == expected
    assert expected.message_uuid is not None
    assert len(source.calls) == 1


def test_expired_failed_reclaimer_reuses_uuid_before_complete_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))

    class CrashOnceNotifier(FakeNotifier):
        supports_message_uuid = True

        def __init__(self) -> None:
            super().__init__()
            self.crash = True

        def send(self, card: Card) -> bool:
            self.cards.append(card)
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt("process stopped after provider accepted")
            return True

    notifier = CrashOnceNotifier()
    engine = _engine(tmp_path, notifier)
    monkeypatch.setattr(pipeline, "_delivery_now", lambda: delivery_start)
    bad_source = FakeSource(_drop_session(_bars(), "QQQM", TARGET))
    with pytest.raises(KeyboardInterrupt):
        run(engine, scheduled, source=bad_source)
    failed_uuid = notifier.cards[-1].message_uuid
    assert failed_uuid is not None
    assert UUID(failed_uuid).version == 5

    monkeypatch.setattr(
        pipeline, "_delivery_now", lambda: delivery_start + timedelta(minutes=11)
    )
    assert not run(engine, scheduled + timedelta(minutes=1), source=FakeSource(_bars()))
    assert notifier.cards[-1].kind is CardKind.ALERT
    assert notifier.cards[-1].message_uuid == failed_uuid

    assert run(engine, scheduled + timedelta(minutes=2), source=FakeSource(_bars()))
    complete_uuid = notifier.cards[-1].message_uuid
    assert complete_uuid is not None
    assert UUID(complete_uuid).version == 5
    assert complete_uuid != failed_uuid

    notifier.cards.clear()
    assert replay(engine)
    assert notifier.cards[0].message_uuid != complete_uuid
    assert dataclasses.replace(
        notifier.cards[0], message_uuid=complete_uuid
    ) == engine.ledger.latest_complete_fear_dca_card()


def test_non_idempotent_notifier_does_not_steal_expired_failed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))

    class AmbiguousNotifier(FakeNotifier):
        def send(self, card: Card) -> bool:
            self.cards.append(card)
            raise KeyboardInterrupt("provider may have accepted")

    notifier = AmbiguousNotifier()
    engine = _engine(tmp_path, notifier)
    monkeypatch.setattr(pipeline, "_delivery_now", lambda: delivery_start)
    with pytest.raises(KeyboardInterrupt):
        run(
            engine,
            scheduled,
            source=FakeSource(_drop_session(_bars(), "QQQM", TARGET)),
        )
    assert len(notifier.cards) == 1

    monkeypatch.setattr(
        pipeline, "_delivery_now", lambda: delivery_start + timedelta(minutes=11)
    )
    assert not run(engine, scheduled + timedelta(minutes=1), source=FakeSource(_bars()))
    assert len(notifier.cards) == 1
    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "FAILED"
    assert stored["send_status"] == "IN_FLIGHT"


def test_idempotent_notifier_does_not_reclaim_after_provider_dedupe_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    delivery_start = datetime(2026, 8, 17, 2, 0, tzinfo=ZoneInfo("UTC"))

    class AmbiguousIdempotentNotifier(FakeNotifier):
        supports_message_uuid = True

        def send(self, card: Card) -> bool:
            self.cards.append(card)
            raise KeyboardInterrupt("provider may have accepted")

    notifier = AmbiguousIdempotentNotifier()
    engine = _engine(tmp_path, notifier)
    monkeypatch.setattr(pipeline, "_delivery_now", lambda: delivery_start)
    with pytest.raises(KeyboardInterrupt):
        run(
            engine,
            scheduled,
            source=FakeSource(_drop_session(_bars(), "QQQM", TARGET)),
        )

    monkeypatch.setattr(
        pipeline, "_delivery_now", lambda: delivery_start + timedelta(minutes=61)
    )
    assert not run(engine, scheduled + timedelta(minutes=1), source=FakeSource(_bars()))
    assert len(notifier.cards) == 1
    stored = engine.ledger.fear_dca_run(TARGET)
    assert stored is not None
    assert stored["status"] == "FAILED"
    assert stored["send_status"] == "IN_FLIGHT"


def test_replay_returns_false_when_no_complete_card(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    assert not replay(_engine(tmp_path, notifier))
    assert notifier.cards == []


def test_engine_exposes_run_and_replay_facades(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = object.__new__(Engine)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "quant_signal.engine.run_fear_dca_pipeline",
        lambda actual, at: calls.append(("run", (actual, at))) or True,
    )
    monkeypatch.setattr(
        "quant_signal.engine.replay_fear_dca_pipeline",
        lambda actual: calls.append(("replay", actual)) or True,
    )
    monkeypatch.setattr(
        "quant_signal.engine.retry_fear_dca_delivery_pipeline",
        lambda actual, at: calls.append(("retry", (actual, at))) or True,
    )

    assert engine.run_fear_dca(now)
    assert engine.retry_fear_dca_delivery(now)
    assert engine.resend_latest_fear_dca()
    assert calls == [
        ("run", (engine, now)),
        ("retry", (engine, now)),
        ("replay", engine),
    ]
