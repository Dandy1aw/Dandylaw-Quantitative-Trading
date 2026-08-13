from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card, CardKind
from quant_signal.pipelines import fear_dca as pipeline
from quant_signal.pipelines.fear_dca import replay, run

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


def _bars(*, periods: int = 119, target: date = TARGET) -> pd.DataFrame:
    sessions = pd.bdate_range(end=target, periods=periods, tz="UTC")
    frames: list[pd.DataFrame] = []
    starts = {"^VIX": 20.0, "^VXN": 30.0, "SPY": 500.0, "QQQM": 200.0}
    for symbol in SYMBOLS:
        closes = np.linspace(starts[symbol], starts[symbol] * 1.2, periods)
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


def _engine(tmp_path: Path, notifier: FakeNotifier) -> SimpleNamespace:
    return SimpleNamespace(
        ledger=SignalLedger(tmp_path / "signals.db"),
        notifier=notifier,
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
    bars = _limit_symbol(_limit_symbol(_bars(), "^VIX", 60), "^VXN", 60)
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


def test_send_exception_is_recorded_after_complete_persistence(
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
    assert stored["send_status"] == "FAILED"
    assert stored["send_error"] == "send broke"


def test_replay_sends_latest_complete_card_without_fetching(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    engine = _engine(tmp_path, notifier)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=ASIA)
    source = FakeSource(_bars())
    assert run(engine, now, source=source)
    expected = notifier.cards[-1]
    notifier.cards.clear()

    assert replay(engine)

    assert notifier.cards == [expected]
    assert len(source.calls) == 1


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

    assert engine.run_fear_dca(now)
    assert engine.resend_latest_fear_dca()
    assert calls == [("run", (engine, now)), ("replay", engine)]
