from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import make_test_settings

from quant_signal.config import (
    HoldingPriceAlertSettings,
    PriceMoveCauseSearchSettings,
)
from quant_signal.pipelines.holding_price_alert import _prior_alerts_from_rows, run
from quant_signal.price_move_research import PriceMoveCause, PriceMoveEvidence
from quant_signal.strategies.base import Direction, Signal, dedup_key


def _bars(last: float) -> pd.DataFrame:
    ts = pd.date_range("2026-08-04T14:00:00Z", periods=31, freq="1min")
    closes = [100.0] * 30 + [last]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [10_000.0] * 31,
        },
        index=pd.MultiIndex.from_product([["AAA"], ts], names=["ticker", "ts"]),
    )
    frame.attrs["feed"] = "test_1m"
    return frame


class _Source:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = 0

    def fetch_minute_bars(self, tickers: list[str], lookback: int) -> pd.DataFrame:
        assert tickers == ["AAA"]
        assert lookback == 45
        self.calls += 1
        return self.frame


class _Ledger:
    def __init__(
        self,
        positions: list[dict[str, object]],
        manual: list[str] | None = None,
    ) -> None:
        self.positions = positions
        self.manual = manual or []
        self.pushed: dict[str, datetime] = {}
        self.inserted: list[tuple[object, bool]] = []
        self.history: list[dict[str, object]] = []
        self.last_history_since: datetime | None = None

    def active_observed_positions(
        self, *, exact_only: bool = False
    ) -> list[dict[str, object]]:
        assert exact_only is True
        return self.positions

    def active_manual_monitors(self) -> list[str]:
        return self.manual

    def last_push_by_key(self, since: datetime) -> dict[str, datetime]:
        return {key: value for key, value in self.pushed.items() if value >= since}

    def pushed_count_since(self, since: datetime, strategy_ids: set[str]) -> int:
        assert strategy_ids == {"holding_price_alert"}
        return sum(value >= since for value in self.pushed.values())

    def pushed_strategy_signals_since(
        self, strategy_id: str, since: datetime
    ) -> list[dict[str, object]]:
        assert strategy_id == "holding_price_alert"
        self.last_history_since = since
        return [
            row
            for row in self.history
            if datetime.fromisoformat(str(row["pushed_at"])) >= since
        ]

    def insert(self, signal: object, pushed: bool, now: datetime) -> int:
        self.inserted.append((signal, pushed))
        if pushed:
            self.pushed[dedup_key(signal)] = now  # type: ignore[arg-type]
            extra = dict(signal.extra or {})  # type: ignore[attr-defined]
            self.history.append(
                {
                    "ticker": signal.ticker,  # type: ignore[attr-defined]
                    "direction": signal.direction.value,  # type: ignore[attr-defined]
                    "pushed_at": now.isoformat(),
                    "extra": extra,
                    "extra_valid": True,
                }
            )
        return len(self.inserted)


class _Notifier:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.cards: list[object] = []
        self.outcomes = list(outcomes or [])

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return self.outcomes.pop(0) if self.outcomes else True


class _Engine:
    def __init__(
        self,
        frame: pd.DataFrame,
        positions: list[dict[str, object]],
        *,
        cause_search: bool = False,
        manual: list[str] | None = None,
    ) -> None:
        self.settings = make_test_settings(
            holding_price_alert=HoldingPriceAlertSettings(
                enabled=True,
                cause_search=PriceMoveCauseSearchSettings(enabled=cause_search),
            )
        )
        self.source = _Source(frame)
        self._intl_source = self.source
        self.news_source = None
        self.ledger = _Ledger(positions, manual)
        self.notifier = _Notifier()


def _position() -> dict[str, object]:
    return {"symbol": "AAA", "qty": "2", "avg_entry_price": "90"}


def test_pipeline_suppresses_same_tier_and_allows_severity_upgrade() -> None:
    now = datetime(2026, 8, 4, 14, 30, tzinfo=UTC)
    engine = _Engine(_bars(102.0), [_position()])

    run(engine, now)  # type: ignore[arg-type]
    run(engine, now)  # type: ignore[arg-type]
    engine.source.frame = _bars(104.0)
    run(engine, now)  # type: ignore[arg-type]

    assert len(engine.notifier.cards) == 2
    assert [pushed for _, pushed in engine.ledger.inserted] == [True, False, True]
    suffixes = [signal.dedup_suffix for signal, _ in engine.ledger.inserted]
    assert suffixes == ["1m:up:t1", "1m:up:t1", "1m:up:t3"]


def test_pipeline_does_not_fetch_without_an_exact_position_snapshot() -> None:
    engine = _Engine(_bars(104.0), [])
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]
    assert engine.source.calls == 0
    assert engine.notifier.cards == []


def test_pipeline_monitors_manual_symbol_without_position_pnl() -> None:
    engine = _Engine(_bars(104.0), [], manual=["AAA"])

    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert engine.source.calls == 1
    assert len(engine.notifier.cards) == 1
    card = engine.notifier.cards[0]
    assert "个股股价异动" in card.title
    assert "持仓语境" not in card.body_md
    assert "相对成本" not in card.body_md


def test_pipeline_searches_only_after_dedup_and_attaches_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_research(  # type: ignore[no-untyped-def]
        signals, settings, *, now, seed_news
    ):
        calls.append([signal.ticker for signal in signals])
        return {
            "AAA": PriceMoveCause(
                ticker="AAA",
                category="company_news",
                confidence="high",
                summary="公司发布重大公告。",
                evidence=(
                    PriceMoveEvidence(
                        title="Announcement",
                        source="Company IR",
                        url="https://example.com/news",
                    ),
                ),
                search_status="ok",
                elapsed_seconds=3.0,
            )
        }

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    now = datetime(2026, 8, 4, 14, 30, tzinfo=UTC)

    run(engine, now)  # type: ignore[arg-type]
    run(engine, now)  # type: ignore[arg-type]

    assert calls == [["AAA"]]
    assert len(engine.notifier.cards) == 1
    assert "Codex实时查因" in engine.notifier.cards[0].body_md
    inserted_signal = engine.ledger.inserted[0][0]
    assert inserted_signal.extra["price_move_cause"]["confidence"] == "high"


def _candidate(
    ticker: str,
    *,
    direction: Direction = Direction.BUY,
    severity: int = 1,
    strength: float = 1.0,
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=direction,
        price=100.0,
        reason="price move",
        strategy_id="holding_price_alert",
        ts=datetime(2026, 8, 4, 14, 30, tzinfo=UTC),
        extra={
            "severity": severity,
            "strength_score": strength,
            "move_pct": 0.02 * strength,
            "threshold_pct": 0.02,
            "observed_at": "2026-08-04T14:30:00+00:00",
        },
    )


def _history_row(
    ticker: str,
    *,
    direction: Direction = Direction.BUY,
    severity: int = 1,
    strength: float | None = 1.0,
    pushed_at: str = "2026-08-04T14:00:00+00:00",
) -> dict[str, object]:
    extra: dict[str, object] = {
        "severity": severity,
        "move_pct": 0.02,
        "threshold_pct": 0.02,
        "alert_kind": "FIRST",
    }
    if strength is not None:
        extra["strength_score"] = strength
    return {
        "ticker": ticker,
        "direction": direction.value,
        "pushed_at": pushed_at,
        "extra": extra,
        "extra_valid": True,
    }


def _patch_candidates(
    monkeypatch: pytest.MonkeyPatch, candidates: list[Signal]
) -> None:
    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.evaluate_holding_price_alerts",
        lambda *args, **kwargs: SimpleNamespace(
            signals=tuple(candidates),
            requested=len(candidates),
            available=len(candidates),
            evaluated=len(candidates),
            stale=0,
            future=0,
            insufficient=0,
            max_bar_age_seconds=0.0,
        ),
    )


def test_pipeline_uses_eastern_midnight_and_legacy_strength_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 5, 3, 30, tzinfo=UTC)  # 08/04 23:30 ET
    engine = _Engine(_bars(102.0), [_position()])
    engine.ledger.history = [_history_row("AAA", strength=None)]
    _patch_candidates(monkeypatch, [_candidate("AAA", severity=1, strength=1.4)])

    run(engine, now)  # type: ignore[arg-type]

    assert engine.ledger.last_history_since == datetime(2026, 8, 4, 4, tzinfo=UTC)
    signal, pushed = engine.ledger.inserted[-1]
    assert pushed is False
    assert signal.extra["suppression_reason"] == "NO_MEANINGFUL_UPGRADE"
    assert signal.extra["ticker_alert_number"] == 2


def test_pipeline_reserves_fifth_slot_and_prioritizes_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()])
    engine.ledger.history = [
        _history_row("UP"),
        _history_row("P1"),
        _history_row("P2"),
        _history_row("P3"),
    ]
    _patch_candidates(
        monkeypatch,
        [
            _candidate("NEW", strength=3.0),
            _candidate("UP", severity=2, strength=1.5),
        ],
    )

    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert [row[0].ticker for row in engine.ledger.inserted] == ["UP", "NEW"]
    assert [row[1] for row in engine.ledger.inserted] == [True, False]
    assert engine.ledger.inserted[0][0].extra["alert_kind"] == "UPGRADE"
    assert (
        engine.ledger.inserted[1][0].extra["suppression_reason"]
        == "GLOBAL_DAILY_CAP"
    )


def test_pipeline_marks_reversal_and_enforces_third_ticker_alert_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()])
    engine.ledger.history = [_history_row("REV", direction=Direction.BUY)]
    _patch_candidates(
        monkeypatch,
        [_candidate("REV", direction=Direction.SELL, strength=1.0)],
    )

    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]
    reversal = engine.ledger.inserted[-1][0]
    assert engine.ledger.inserted[-1][1] is True
    assert reversal.extra["alert_kind"] == "REVERSAL"
    assert reversal.extra["ticker_alert_number"] == 2

    _patch_candidates(
        monkeypatch,
        [_candidate("REV", direction=Direction.BUY, severity=3, strength=3.0)],
    )
    run(engine, datetime(2026, 8, 4, 14, 31, tzinfo=UTC))  # type: ignore[arg-type]

    third = engine.ledger.inserted[-1][0]
    assert engine.ledger.inserted[-1][1] is False
    assert third.extra["suppression_reason"] == "TICKER_DAILY_CAP"
    assert third.extra["ticker_alert_number"] == 3


def test_failed_send_does_not_consume_slot_and_later_candidate_is_reselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()])
    engine.notifier = _Notifier([False, True])
    engine.ledger.history = [_history_row("P1"), _history_row("P2"), _history_row("P3")]
    _patch_candidates(
        monkeypatch,
        [_candidate("A", strength=2.0), _candidate("B", strength=1.0)],
    )

    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert [row[0].ticker for row in engine.ledger.inserted] == ["A", "B"]
    assert [row[1] for row in engine.ledger.inserted] == [False, True]
    assert "send_error" in engine.ledger.inserted[0][0].extra
    assert engine.ledger.inserted[1][0].extra["alert_kind"] == "FIRST"


def test_research_only_runs_for_candidates_actually_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    engine.ledger.history = [_history_row("AAA")]
    _patch_candidates(
        monkeypatch,
        [_candidate("AAA", strength=1.4), _candidate("BBB", strength=1.0)],
    )

    def fake_research(signals, settings, *, now, seed_news):  # type: ignore[no-untyped-def]
        calls.append([signal.ticker for signal in signals])
        return {}

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert calls == [["BBB"]]
    assert [row[1] for row in engine.ledger.inserted] == [True, False]


def test_research_batches_all_currently_approved_candidates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    engine.ledger.history = [_history_row("P1"), _history_row("P2")]
    _patch_candidates(
        monkeypatch,
        [
            _candidate("A", strength=2.0),
            _candidate("B", strength=1.5),
            _candidate("C", strength=1.0),
        ],
    )

    def fake_research(signals, settings, *, now, seed_news):  # type: ignore[no-untyped-def]
        calls.append([signal.ticker for signal in signals])
        return {}

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert calls == [["A", "B"]]
    assert [row[0].ticker for row in engine.ledger.inserted] == ["A", "B", "C"]
    assert [row[1] for row in engine.ledger.inserted] == [True, True, False]


def test_failed_send_only_researches_newly_promoted_candidate_in_second_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    engine.notifier = _Notifier([False, True])
    engine.ledger.history = [_history_row("P1"), _history_row("P2"), _history_row("P3")]
    _patch_candidates(
        monkeypatch,
        [_candidate("A", strength=2.0), _candidate("B", strength=1.0)],
    )

    def fake_research(signals, settings, *, now, seed_news):  # type: ignore[no-untyped-def]
        calls.append([signal.ticker for signal in signals])
        return {}

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert calls == [["A"], ["B"]]
    assert [row[1] for row in engine.ledger.inserted] == [False, True]


def test_history_parser_fails_closed_for_malformed_direction_and_extra() -> None:
    rows = [
        {
            "ticker": "AAA",
            "direction": "sideways",
            "pushed_at": "2026-08-04T14:00:00+00:00",
            "extra": {},
            "extra_valid": False,
        }
    ]

    history, blocked_tickers, history_uncertain, issues = _prior_alerts_from_rows(
        rows, datetime(2026, 8, 4, 4, tzinfo=UTC)
    )

    assert len(history) == 1  # malformed pushed rows still consume global quota
    assert history[0].severity == 3
    assert history[0].strength_score >= 1.5
    assert blocked_tickers == {"AAA"}
    assert history_uncertain is False
    assert issues


@pytest.mark.parametrize(
    "extra",
    [
        {"severity": 1, "strength_score": 10**10000},
        {
            "severity": 1,
            "move_pct": 10**10000,
            "threshold_pct": 0.02,
        },
        {
            "severity": 1,
            "move_pct": 0.02,
            "threshold_pct": 10**10000,
        },
        {
            "severity": 1,
            "move_pct": 1.0,
            "threshold_pct": 5e-324,
        },
    ],
)
def test_history_parser_conservatively_accepts_extreme_json_numbers(
    extra: dict[str, object],
) -> None:
    history, blocked_tickers, history_uncertain, issues = _prior_alerts_from_rows(
        [
            {
                "ticker": "AAA",
                "direction": "buy",
                "pushed_at": "2026-08-04T14:00:00+00:00",
                "extra": extra,
                "extra_valid": True,
            }
        ],
        datetime(2026, 8, 4, 4, tzinfo=UTC),
    )

    assert len(history) == 1
    assert history[0].strength_score == 1.5
    assert blocked_tickers == set()
    assert history_uncertain is False
    assert any("conservative_strength" in issue for issue in issues)


def test_pipeline_does_not_crash_on_extreme_valid_history_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()])
    row = _history_row("OLD")
    row["extra"] = {"severity": 1, "strength_score": 10**10000}
    engine.ledger.history = [row]
    _patch_candidates(monkeypatch, [_candidate("NEW")])

    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert engine.ledger.inserted[-1][1] is True
    assert engine.ledger.inserted[-1][0].extra["ticker_alert_number"] == 1


@pytest.mark.parametrize("ticker", ["", None, 123])
def test_unrecoverable_history_ticker_marks_global_history_uncertain(
    ticker: object,
) -> None:
    history, blocked_tickers, history_uncertain, issues = _prior_alerts_from_rows(
        [
            {
                "ticker": ticker,
                "direction": "buy",
                "pushed_at": "2026-08-04T14:00:00+00:00",
                "extra": {"severity": 1, "strength_score": 1.0},
                "extra_valid": True,
            }
        ],
        datetime(2026, 8, 4, 4, tzinfo=UTC),
    )

    assert len(history) == 1
    assert blocked_tickers == set()
    assert history_uncertain is True
    assert any("invalid_ticker" in issue for issue in issues)


def test_pipeline_fails_closed_for_all_candidates_when_history_ticker_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    engine.ledger.history = [
        {
            "ticker": "",
            "direction": "buy",
            "pushed_at": "2026-08-04T14:00:00+00:00",
            "extra": {"severity": 1, "strength_score": 1.0},
            "extra_valid": True,
        }
    ]
    _patch_candidates(monkeypatch, [_candidate("A"), _candidate("B")])
    research_calls: list[list[str]] = []

    def fake_research(signals, settings, *, now, seed_news):  # type: ignore[no-untyped-def]
        research_calls.append([signal.ticker for signal in signals])
        return {}

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert engine.notifier.cards == []
    assert research_calls == []
    assert len(engine.ledger.inserted) == 2
    assert all(not pushed for _, pushed in engine.ledger.inserted)
    assert all(
        signal.extra["suppression_reason"] == "HISTORY_UNCERTAIN"
        and "history_reconstruction_reason" in signal.extra
        for signal, _ in engine.ledger.inserted
    )


def test_pipeline_caps_failed_delivery_attempts_and_audits_every_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(_bars(102.0), [_position()], cause_search=True)
    engine.notifier = _Notifier([False] * 20)
    candidates = [_candidate(f"T{i:02d}", strength=2.0 - i / 100) for i in range(20)]
    _patch_candidates(monkeypatch, candidates)
    research_calls: list[list[str]] = []

    def fake_research(signals, settings, *, now, seed_news):  # type: ignore[no-untyped-def]
        research_calls.append([signal.ticker for signal in signals])
        return {}

    monkeypatch.setattr(
        "quant_signal.pipelines.holding_price_alert.research_price_move_causes",
        fake_research,
    )
    run(engine, datetime(2026, 8, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert len(engine.notifier.cards) == 5
    assert len(research_calls) <= 5
    assert len(engine.ledger.inserted) == 20
    assert len({signal.ticker for signal, _ in engine.ledger.inserted}) == 20
    assert all(not pushed for _, pushed in engine.ledger.inserted)
    capped = [
        signal
        for signal, _ in engine.ledger.inserted
        if signal.extra.get("suppression_reason") == "DELIVERY_ATTEMPT_CAP"
    ]
    assert len(capped) == 15
