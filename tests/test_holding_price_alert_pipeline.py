from datetime import UTC, datetime

import pandas as pd
import pytest
from conftest import make_test_settings

from quant_signal.config import (
    HoldingPriceAlertSettings,
    PriceMoveCauseSearchSettings,
)
from quant_signal.pipelines.holding_price_alert import run
from quant_signal.price_move_research import PriceMoveCause, PriceMoveEvidence
from quant_signal.strategies.base import dedup_key


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

    def insert(self, signal: object, pushed: bool, now: datetime) -> int:
        self.inserted.append((signal, pushed))
        if pushed:
            self.pushed[dedup_key(signal)] = now  # type: ignore[arg-type]
        return len(self.inserted)


class _Notifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


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
