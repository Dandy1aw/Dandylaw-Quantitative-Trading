from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.news import NewsArticle
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.news_store import NewsStore


class Notifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def bars() -> pd.DataFrame:
    idx = pd.bdate_range("2025-08-01", periods=210, tz="UTC")
    close = np.concatenate([np.linspace(80.0, 100.0, 205), [90.0, 91.0, 92.0, 95.0, 96.0]])
    volume = np.full(210, 1_000_000.0)
    volume[205] = 2_000_000.0
    frame = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": volume},
        index=pd.MultiIndex.from_product([["MU"], idx], names=["ticker", "ts"]),
    )
    frame.loc[("MU", idx[205]), "low"] = 88.0
    return frame


class PriceSource:
    def __init__(self, data: pd.DataFrame) -> None:
        self.data = data

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return self.data

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


class NewsSource:
    def __init__(self, item: NewsArticle) -> None:
        self.item = item

    def fetch(self, symbols, start, end):  # type: ignore[no-untyped-def]
        return [self.item]


def _engine(
    tmp_path: Path, with_news: bool = True, news_days_before: int = 0
) -> tuple[Engine, Notifier]:
    data = bars()
    shock_at = data.xs("MU", level="ticker").index[205].to_pydatetime()
    news_at = shock_at.replace(hour=14)
    if news_days_before:
        news_at = shock_at.replace(hour=22) - pd.Timedelta(days=news_days_before)
    item = NewsArticle(
        "n1", news_at, news_at, ("MU",),
        "Quarterly revenue misses estimates on one-time charge", "", "", "", "benzinga",
    )
    settings = load_settings().model_copy(update={"tickers": {"MU": {"asset_type": "STOCK"}},
                                                   "watchlist": []})
    notifier = Notifier()
    engine = Engine(
        settings, BarStore(tmp_path / "bars.duckdb"), PriceSource(data),
        SignalLedger(tmp_path / "signals.db"), notifier,
        news_source=NewsSource(item) if with_news else None,
        news_store=NewsStore(tmp_path / "news.db"),
    )
    return engine, notifier


def test_pipeline_safely_skips_without_news_source(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path, with_news=False)
    engine.run_negative_overreaction(datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert notifier.cards == []


def test_pipeline_pushes_repairable_confirmed_case_once(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path)
    now = bars().index.get_level_values("ts")[-1].to_pydatetime().replace(hour=22)

    engine.run_negative_overreaction(now)
    engine.run_negative_overreaction(now)

    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "MU" in body and "earnings_miss" in body


def test_pipeline_matches_previous_evening_news_to_next_session_shock(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path, news_days_before=1)
    now = bars().index.get_level_values("ts")[-1].to_pydatetime().replace(hour=22)
    engine.run_negative_overreaction(now)
    assert len(notifier.cards) == 1
