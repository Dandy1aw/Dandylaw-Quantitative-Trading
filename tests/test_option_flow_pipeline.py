from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_test_settings
from quant_signal.config import OptionFlowSettings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.options_flow import OptionContractVolume, OptionFlowSnapshot, scan_slot

NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)  # 10:15 ET
ROOTS = ("AAPL", "NVDA", "MSFT", "TSLA", "AMZN", "META", "GOOGL", "AMD", "MU", "INTC", "PLTR", "NFLX")


def scan(at: datetime, *, changed: bool = False) -> OptionFlowSnapshot:
    rows: list[OptionContractVolume] = []
    for side in ("call", "put"):
        marker = "C" if side == "call" else "P"
        ordered = (("NFLX",) + ROOTS[:-1]) if changed and side == "call" else ROOTS
        for rank, root in enumerate(ordered, start=1):
            original_rank = ROOTS.index(root) + 1
            volume = 8_000 + (13 - original_rank) * 500
            if changed and side == "call" and root == "NFLX":
                volume = 20_000
            strike = Decimal(200 + original_rank)
            rows.append(
                OptionContractVolume(
                    contract_symbol=f"{root}260717{marker}{int(strike * 1000):08d}",
                    underlying=root,
                    side=side,
                    expiration=date(2026, 7, 17),
                    strike=strike,
                    volume=volume,
                    rank=rank,
                    venues=("cone", "ctwo", "exo", "opt"),
                    captured_at=at,
                )
            )
    return OptionFlowSnapshot(
        slot=scan_slot(at),
        captured_at=at,
        provider="cboe-four-venues",
        venue_coverage=1.0,
        rows=tuple(rows),
    )


class StockSource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return pd.DataFrame()

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


class OptionSource:
    def __init__(self, changed_on: set[int] | None = None, failure: Exception | None = None) -> None:
        self.calls = 0
        self.changed_on = changed_on or set()
        self.failure = failure

    def fetch(self, now: datetime) -> OptionFlowSnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return scan(now, changed=self.calls in self.changed_on)


class Notifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.cards: list[Card] = []
        self.results = list(results or [])

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return self.results.pop(0) if self.results else True


class BrokenEnricher:
    def enrich(self, rows, now):  # type: ignore[no-untyped-def]
        raise RuntimeError("enrichment down")


def make_engine(
    tmp_path: Path,
    *,
    option_source: OptionSource | None = None,
    notifier: Notifier | None = None,
    option_enricher: object | None = None,
    policy: OptionFlowSettings | None = None,
) -> tuple[Engine, Notifier]:
    settings = make_test_settings(
        option_flow=policy
        or OptionFlowSettings(enabled=True, min_volume=5_000, cooldown_minutes=60)
    )
    output = notifier or Notifier()
    engine = Engine(
        settings,
        BarStore(tmp_path / "bars.duckdb"),
        StockSource(),
        SignalLedger(tmp_path / "signals.db"),
        output,
        option_flow_source=option_source or OptionSource(),
        option_flow_enricher=option_enricher,  # type: ignore[arg-type]
    )
    return engine, output


def test_first_scan_queues_and_sends_one_baseline(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)
    engine.run_option_flow(NOW)
    assert len(notifier.cards) == 1
    assert "Cboe四市场" in notifier.cards[0].title
    assert engine.ledger.option_flow_alert_count(NOW.date()) == 1
    assert engine.ledger.due_option_flow_alerts(NOW) == []


def test_ordinary_rank_noise_is_stored_without_push(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=15))
    assert len(notifier.cards) == 1
    assert engine.ledger.latest_option_flow_snapshot(NOW.date()) is not None


def test_material_change_pushes_after_cooldown(tmp_path: Path) -> None:
    source = OptionSource(changed_on={2})
    engine, notifier = make_engine(tmp_path, option_source=source)
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=60))
    assert len(notifier.cards) == 2
    assert notifier.cards[-1].kind.value == "signal"
    assert "NFLX" in notifier.cards[-1].body_md


def test_baseline_card_does_not_start_change_cooldown(tmp_path: Path) -> None:
    # 冷却只约束“变化卡之间”的间隔；10:00 基线不该封锁 10:15 的实质变化。
    source = OptionSource(changed_on={2})
    engine, notifier = make_engine(tmp_path, option_source=source)
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=15))
    assert len(notifier.cards) == 2
    assert "NFLX" in notifier.cards[-1].body_md


def test_daily_limit_reserves_one_slot_for_close(tmp_path: Path) -> None:
    source = OptionSource(changed_on={2})
    policy = OptionFlowSettings(enabled=True, max_alerts_per_day=2, min_volume=5_000)
    engine, notifier = make_engine(tmp_path, option_source=source, policy=policy)
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=60))
    engine.run_option_flow(datetime(2026, 7, 10, 20, 20, tzinfo=UTC), force_summary=True)
    assert len(notifier.cards) == 2
    assert "收盘榜" in notifier.cards[-1].body_md


def test_failed_send_is_retried_from_option_outbox(tmp_path: Path) -> None:
    notifier = Notifier([False, True])
    engine, _ = make_engine(tmp_path, notifier=notifier)
    engine.run_option_flow(NOW)
    assert len(engine.ledger.due_option_flow_alerts(NOW + timedelta(minutes=4))) == 0
    engine.run_option_flow(NOW + timedelta(minutes=15))
    assert len(notifier.cards) == 2
    assert engine.ledger.due_option_flow_alerts(NOW + timedelta(minutes=15)) == []


def test_delivery_only_run_drains_outbox_without_fetching(tmp_path: Path) -> None:
    # 收盘卡 16:20 发送失败后，傍晚的 drain 任务只重试 outbox，不再抓取数据。
    notifier = Notifier([False])
    source = OptionSource()
    engine, _ = make_engine(tmp_path, option_source=source, notifier=notifier)
    engine.run_option_flow(NOW)
    fetches = source.calls
    assert engine.ledger.due_option_flow_alerts(NOW + timedelta(minutes=6)) != []

    engine.run_option_flow_delivery(NOW + timedelta(minutes=6))

    assert source.calls == fetches
    assert len(notifier.cards) == 2
    assert engine.ledger.due_option_flow_alerts(NOW + timedelta(minutes=6)) == []


def test_unconfigured_enricher_is_not_labeled_as_failure(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)  # option_enricher=None
    engine.run_option_flow(NOW)
    assert "未配置Alpaca补全" in notifier.cards[0].body_md
    assert "失败" not in notifier.cards[0].body_md


def test_enrichment_failure_degrades_card_but_source_failure_reaches_health(
    tmp_path: Path,
) -> None:
    engine, notifier = make_engine(tmp_path, option_enricher=BrokenEnricher())
    engine.run_option_flow(NOW)
    assert "Alpaca补全失败" in notifier.cards[0].body_md

    broken, _ = make_engine(
        tmp_path / "broken", option_source=OptionSource(failure=RuntimeError("Cboe down"))
    )
    with pytest.raises(RuntimeError, match="Cboe down"):
        broken.run_option_flow(NOW)


def test_disabled_option_flow_does_nothing(tmp_path: Path) -> None:
    source = OptionSource()
    engine, notifier = make_engine(
        tmp_path,
        option_source=source,
        policy=OptionFlowSettings(enabled=False),
    )
    engine.run_option_flow(NOW)
    assert source.calls == 0 and notifier.cards == []
