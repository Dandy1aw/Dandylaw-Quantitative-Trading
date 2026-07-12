"""收盘复盘管道：截图持仓 + 日线/实时价兜底 + 信号计数 → 一张卡。"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from conftest import make_test_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.pipelines.postmarket import run
from quant_signal.portfolio_import import (
    ExtractedAccount,
    ExtractedPosition,
    PortfolioExtraction,
    validate_extraction,
)
from quant_signal.strategies.base import Direction, Signal

NOW = datetime(2026, 7, 10, 20, 30, tzinfo=UTC)  # 16:30 ET
SESSION = date(2026, 7, 10)
OBSERVED_AT = datetime(2026, 7, 10, 1, 55, tzinfo=timezone(timedelta(hours=8)))


class StockSource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return pd.DataFrame()

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


class Notifier:
    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


class SpotSource:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    def fetch_live_price(self, ticker: str) -> float | None:
        return self.prices.get(ticker)

    def fetch_live_prices(self, tickers):  # type: ignore[no-untyped-def]
        return {t: self.prices.get(t) for t in tickers}

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


def seed_position(
    ledger: SignalLedger,
    *,
    symbol: str = "MU",
    qty: str = "1",
    avg: str = "100",
    current: str = "104",
    mv: str = "104",
) -> None:
    extraction = PortfolioExtraction(
        account=ExtractedAccount(
            equity="5995.52",
            market_value=mv,
            cash="5891.52",
            buying_power="5891.52",
            frozen_cash="0",
            processing_cash="0",
            total_unrealized_pnl="0",
            day_pnl="0",
            currency="USD",
            reported_position_count=1,
            observed_at=OBSERVED_AT,
        ),
        positions=(
            ExtractedPosition(
                symbol=symbol,
                qty=qty,
                avg_entry_price=avg,
                current_price=current,
                market_value=mv,
                weight_pct="1.73",
                pnl="4",
                pnl_pct="4.0",
            ),
        ),
    )
    result = validate_extraction(
        extraction,
        image_sha256="a" * 64,
        uploaded_at=OBSERVED_AT,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
        require_account_reconciliation=False,
    )
    assert ledger.save_portfolio_import(result) is True


def write_closes(store: BarStore, symbol: str, closes: dict[str, float]) -> None:
    idx = pd.MultiIndex.from_tuples(
        [(symbol, pd.Timestamp(day, tz="UTC")) for day in closes],
        names=["ticker", "ts"],
    )
    store.write_daily_bars(
        pd.DataFrame(
            {
                "open": list(closes.values()),
                "high": list(closes.values()),
                "low": list(closes.values()),
                "close": list(closes.values()),
                "volume": 1_000_000,
            },
            index=idx,
        ),
        source="test",
    )


def make_engine(
    tmp_path: Path, *, live: dict[str, float] | None = None
) -> tuple[Engine, Notifier]:
    notifier = Notifier()
    engine = Engine(
        make_test_settings(universe=["MU", "SMH"]),
        BarStore(tmp_path / "bars.duckdb"),
        StockSource(),
        SignalLedger(tmp_path / "signals.db"),
        notifier,
    )
    engine._intl_source = SpotSource(live or {})  # type: ignore[assignment]
    return engine, notifier


def test_uses_todays_bar_when_present(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)
    seed_position(engine.ledger, avg="100")
    write_closes(engine.store, "MU", {"2026-07-09": 100.0, "2026-07-10": 105.0})
    run(engine, NOW)

    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md
    assert "收盘复盘" in notifier.cards[0].title
    assert "+5.0%" in body       # 当日 105/100-1
    assert "105.00" in body      # 收盘价
    assert "5.0%" in body        # 持仓盈亏 105/100-1（同数值）


def test_falls_back_to_live_price_when_todays_bar_missing(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path, live={"MU": 103.0})
    seed_position(engine.ledger, avg="100")
    write_closes(engine.store, "MU", {"2026-07-09": 100.0})
    run(engine, NOW)

    body = notifier.cards[0].body_md
    assert "+3.0%" in body       # 当日 103/100-1（实时价兜底）
    assert "103.00" in body


def test_stale_bars_without_live_price_omit_day_change(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)  # 无实时价
    seed_position(engine.ledger, avg="90")   # 盈亏 +11.1%，与当日列区分
    write_closes(engine.store, "MU", {"2026-07-09": 100.0})
    run(engine, NOW)

    body = notifier.cards[0].body_md
    assert "MU" in body
    assert "+11.1%" in body      # 持仓盈亏仍给（基于最近收盘）
    assert "+0.0%" not in body   # 不能把昨收/昨收 当成当日涨跌


def test_signal_tally_counts_distinct_not_intraday_repeats(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)
    for minutes in (0, 5, 10):  # 盘中同一信号每 5 分钟重复入账
        engine.ledger.insert(
            Signal(
                ticker="NVDA",
                direction=Direction.BUY,
                price=100.0,
                reason="r",
                strategy_id="breakout_20d",
                ts=NOW + timedelta(minutes=minutes),
            ),
            pushed=True,
            now=NOW + timedelta(minutes=minutes),
        )
    run(engine, NOW)

    body = notifier.cards[0].body_md
    assert "BUY 1" in body
    assert "306" not in body and "BUY 3" not in body
    assert "已推送 1" in body   # 推送数同样去重，不能大于信号总数


def test_partial_positions_fall_back_to_screenshot_pnl(tmp_path: Path) -> None:
    """截图缺股数/成本(precision=ESTIMATED)时退回截图时点盈亏并加 * 标注。"""
    engine, notifier = make_engine(tmp_path, live={"MU": 105.0})
    extraction = PortfolioExtraction(
        account=ExtractedAccount(
            equity="5995.52",
            market_value="4244.15",
            cash="1751.13",
            buying_power="3474.15",
            frozen_cash="0",
            processing_cash="0",
            total_unrealized_pnl="-108.04",
            day_pnl="68.33",
            currency="USD",
            reported_position_count=6,
            observed_at=OBSERVED_AT,
        ),
        positions=(
            ExtractedPosition(
                symbol="MU", pnl="166.82", pnl_pct="20.23", weight_pct="16.53"
            ),
        ),
    )
    result = validate_extraction(
        extraction,
        image_sha256="b" * 64,
        uploaded_at=OBSERVED_AT,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )
    assert engine.ledger.save_portfolio_import(result) is True
    run(engine, NOW)

    body = notifier.cards[0].body_md
    assert "+20.2%*" in body
    assert "截图时点" in body


def test_no_positions_still_sends_signal_tally(tmp_path: Path) -> None:
    engine, notifier = make_engine(tmp_path)
    engine.ledger.insert(
        Signal(
            ticker="NVDA",
            direction=Direction.BUY,
            price=100.0,
            reason="r",
            strategy_id="breakout_20d",
            ts=NOW,
        ),
        pushed=True,
        now=NOW,
    )
    run(engine, NOW)

    body = notifier.cards[0].body_md
    assert "无持仓" in body
    assert "BUY 1" in body
