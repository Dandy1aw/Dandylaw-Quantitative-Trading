from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

from quant_signal.pipelines.dataqa import run

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _frame(tickers: list[str]) -> pd.DataFrame:
    index = pd.MultiIndex.from_tuples(
        [(ticker, pd.Timestamp("2026-07-28", tz="UTC")) for ticker in tickers],
        names=["ticker", "ts"],
    )
    return pd.DataFrame({"close": [100.0] * len(tickers)}, index=index)


class Source:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        self.calls.append(list(tickers))
        if self.fail_on and self.fail_on in tickers:
            raise RuntimeError(f"failed {self.fail_on}")
        return _frame(list(tickers))


class Provider:
    def load(self, now):  # type: ignore[no-untyped-def]
        return SimpleNamespace(symbols=("NVDA", "MSFT"))


class Notifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def _engine(primary: Source, secondary: Source) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            data_source="alpaca",
            universe=["SPY"],
            international_tickers=[],
            data_qa=SimpleNamespace(
                min_coverage=0.98,
                divergence_threshold=0.005,
                batch_size=50,
                lookback_days=7,
            ),
        ),
        source=primary,
        _intl_source=secondary,
        index_universe_provider=Provider(),
        notifier=Notifier(),
    )


def test_dataqa_includes_dynamic_index_members() -> None:
    primary = Source()
    secondary = Source()

    assert run(_engine(primary, secondary), NOW) is True  # type: ignore[arg-type]

    assert primary.calls == [["MSFT", "NVDA", "SPY"]]
    assert secondary.calls == [["MSFT", "NVDA", "SPY"]]


def test_dataqa_returns_failure_when_batch_coverage_is_insufficient(
) -> None:
    primary = Source(fail_on="SPY")
    secondary = Source()
    engine = _engine(primary, secondary)
    engine.settings.data_qa.batch_size = 2

    assert run(engine, NOW) is False  # type: ignore[arg-type]
    assert primary.calls == [["MSFT", "NVDA"], ["SPY"]]
    assert secondary.calls == [["MSFT", "NVDA"], ["SPY"]]


def test_dataqa_uses_configured_production_coverage_threshold() -> None:
    class PartialSource(Source):
        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            self.calls.append(list(tickers))
            return _frame(list(tickers)[:-1])

    primary = PartialSource()
    secondary = PartialSource()
    engine = _engine(primary, secondary)

    assert run(engine, NOW) is False  # type: ignore[arg-type]

    engine.settings.data_qa.min_coverage = 0.60
    assert run(engine, NOW) is True  # type: ignore[arg-type]
