"""管道+卡片：持仓期权情报（覆盖集合、降级、落库、发送、📌交叉标记）。"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from conftest import make_test_settings
from quant_signal.config import OptionIntelSettings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import option_intel_card
from quant_signal.options_intel import (
    OptionChainContract,
    OptionChainFetchResult,
    OptionIntel,
)
from quant_signal.position_tactical import (
    OpexContext,
    OptionStructure,
    PositionTacticalAnalysis,
    WeeklyTechnical,
)
from quant_signal.pipelines.option_intel import build_intel, holdings_universe, run

NOW = datetime(2026, 7, 10, 20, 40, tzinfo=UTC)  # 16:40 ET
SESSION = date(2026, 7, 10)


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


def chain_contract(
    symbol: str = "MU", *, side: str = "call", strike: str = "100",
    expiration: date = date(2026, 7, 24),
) -> OptionChainContract:
    marker = "C" if side == "call" else "P"
    return OptionChainContract(
        contract_symbol=f"{symbol}{expiration:%y%m%d}{marker}{int(Decimal(strike) * 1000):08d}",
        underlying=symbol,
        side=side,  # type: ignore[arg-type]
        expiration=expiration,
        strike=Decimal(strike),
        bid=Decimal("2.0"),
        ask=Decimal("2.2"),
        implied_volatility=0.5,
        day_volume=100,
        open_interest=500,
    )


class ChainSource:
    def __init__(self, chains: dict[str, OptionChainFetchResult] | None = None,
                 failure: Exception | None = None) -> None:
        self.chains = chains or {}
        self.failure = failure
        self.requested: list[str] = []

    def fetch_chain(self, underlying: str, *, session: date, max_expiry_days: int) -> OptionChainFetchResult:
        self.requested.append(underlying)
        if self.failure is not None:
            raise self.failure
        return self.chains.get(
            underlying, OptionChainFetchResult(contracts=(), truncated=False)
        )


class SpotSource:
    """替身 engine._fetch_live_price 的 yfinance 源。"""

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    def fetch_live_price(self, ticker: str) -> float | None:
        return self.prices.get(ticker)

    def fetch_live_prices(self, tickers):  # type: ignore[no-untyped-def]
        return {t: self.prices.get(t) for t in tickers}

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


def make_engine(
    tmp_path: Path,
    *,
    chain_source: ChainSource | None = None,
    spot_prices: dict[str, float] | None = None,
    settings_override: OptionIntelSettings | None = None,
) -> tuple[Engine, Notifier, ChainSource]:
    settings = make_test_settings(
        universe=["MU", "SMH"],
        option_intel=settings_override or OptionIntelSettings(enabled=True),
    )
    notifier = Notifier()
    source = chain_source or ChainSource(
        chains={
            "MU": OptionChainFetchResult(
                contracts=(
                    chain_contract("MU", side="call"),
                    chain_contract("MU", side="put", strike="95"),
                ),
                truncated=False,
            )
        }
    )
    engine = Engine(
        settings,
        BarStore(tmp_path / "bars.duckdb"),
        StockSource(),
        SignalLedger(tmp_path / "signals.db"),
        notifier,
        option_chain_source=source,
    )
    engine._intl_source = SpotSource(spot_prices or {"MU": 100.0})  # type: ignore[assignment]
    return engine, notifier, source


def test_holdings_universe_unions_and_filters(tmp_path: Path) -> None:
    engine, _, _ = make_engine(tmp_path)
    engine.ledger.set_holdings("momentum_rotation", ["MU", "000660.KS"])
    universe = holdings_universe(engine)
    assert universe == ["MU"]  # 非美股剔除


def test_holdings_universe_truncates(tmp_path: Path) -> None:
    engine, _, _ = make_engine(
        tmp_path, settings_override=OptionIntelSettings(enabled=True, max_tickers=1)
    )
    engine.ledger.set_holdings("momentum_rotation", ["SMH", "MU"])
    assert holdings_universe(engine) == ["MU"]


def test_run_sends_card_and_persists(tmp_path: Path) -> None:
    engine, notifier, source = make_engine(tmp_path)
    engine.ledger.set_holdings("momentum_rotation", ["MU"])
    run(engine, NOW)

    assert source.requested == ["MU"]
    assert len(notifier.cards) == 1
    card = notifier.cards[0]
    assert "持仓期权情报" in card.title
    assert "MU" in card.body_md
    assert "预期波动" in card.body_md
    assert engine.ledger.option_intel_history("MU")


def test_build_intel_reuses_one_chain_and_loads_long_weekly_history(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    engine, _, source = make_engine(tmp_path)
    starts: list[datetime | None] = []

    def read_daily_bars(
        tickers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        bar_state: str | None = "final",
    ) -> pd.DataFrame:
        del tickers, end, bar_state
        starts.append(start)
        index = pd.MultiIndex.from_arrays(
            [[], []], names=["ticker", "ts"]
        )
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=index,
        )

    monkeypatch.setattr(engine.store, "read_daily_bars", read_daily_bars)

    intel = build_intel(engine, "MU", NOW)

    assert intel is not None
    assert intel.tactical is not None
    assert intel.tactical.technical.state == "DATA_INSUFFICIENT"
    assert source.requested == ["MU"]
    assert starts and starts[0] is not None
    assert starts[0] <= NOW - timedelta(days=420)


def test_run_skips_send_when_no_symbol_has_data(tmp_path: Path) -> None:
    engine, notifier, _ = make_engine(
        tmp_path, chain_source=ChainSource(), spot_prices={"MU": 100.0}
    )
    engine.ledger.set_holdings("momentum_rotation", ["MU"])
    run(engine, NOW)
    assert notifier.cards == []
    assert not engine.ledger.option_intel_history("MU")


def test_run_single_symbol_failure_does_not_block_others(tmp_path: Path) -> None:
    class FlakyChain(ChainSource):
        def fetch_chain(self, underlying: str, *, session: date, max_expiry_days: int) -> OptionChainFetchResult:
            if underlying == "SMH":
                raise RuntimeError("boom")
            return super().fetch_chain(
                underlying, session=session, max_expiry_days=max_expiry_days
            )

    source = FlakyChain(
        chains={
            "MU": OptionChainFetchResult(
                contracts=(
                    chain_contract("MU", side="call"),
                    chain_contract("MU", side="put", strike="95"),
                ),
                truncated=False,
            )
        }
    )
    engine, notifier, _ = make_engine(
        tmp_path, chain_source=source, spot_prices={"MU": 100.0, "SMH": 280.0}
    )
    engine.ledger.set_holdings("momentum_rotation", ["MU", "SMH"])
    run(engine, NOW)
    assert len(notifier.cards) == 1
    assert "MU" in notifier.cards[0].body_md


def test_run_disabled_is_noop(tmp_path: Path) -> None:
    engine, notifier, source = make_engine(
        tmp_path, settings_override=OptionIntelSettings(enabled=False)
    )
    engine.ledger.set_holdings("momentum_rotation", ["MU"])
    run(engine, NOW)
    assert notifier.cards == []
    assert source.requested == []


def intel(symbol: str = "MU", **overrides: object) -> OptionIntel:
    base = OptionIntel(
        symbol=symbol,
        spot=Decimal("100"),
        session=SESSION,
        expected_move_pct=0.062,
        expected_move_expiry=date(2026, 7, 24),
        earnings_move_pct=0.114,
        earnings_move_expiry=date(2026, 8, 7),
        atm_iv=0.58,
        realized_vol_20d=0.41,
        pc_volume_ratio=0.71,
        pc_oi_ratio=0.85,
        top_oi_strikes=(),
        earnings_date=date(2026, 7, 28),
        data_note=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def tactical(symbol: str = "MU") -> PositionTacticalAnalysis:
    return PositionTacticalAnalysis(
        symbol=symbol,
        spot=Decimal("100"),
        technical=WeeklyTechnical(
            state="OVERSOLD_AT_LOWER_BAND",
            weekly_rsi=28.4,
            bollinger_lower=92.0,
            bollinger_mid=110.0,
            bollinger_upper=128.0,
            middle_slope_4w=-0.01,
            weekly_atr=4.0,
            reversal_confirmed=True,
            latest_week_partial=False,
            recent_four_week_low=94.0,
            history_weeks=30,
        ),
        options=OptionStructure(
            expiry=date(2026, 7, 17),
            put_wall=Decimal("95"),
            call_wall=Decimal("105"),
            max_pain=Decimal("100"),
            put_buffer_pct=0.05,
            call_upside_pct=0.05,
            gamma_pin_score=0.40,
            dealer_gamma_state="UNKNOWN",
            reliability="HIGH",
            oi_coverage=0.90,
            quote_coverage=0.80,
            truncated=False,
        ),
        opex=OpexContext(date(2026, 7, 17), 0, "OPEX_DAY"),
        conclusion="TACTICAL_REBOUND",
        invalidation_price=Decimal("93"),
        target_1=Decimal("105"),
        target_2=Decimal("110"),
        holding_note="月度OPEX当日不追单，避免依据旧墙位隔夜加仓。",
    )


class TestOptionIntelCard:
    def test_renders_fields(self) -> None:
        card = option_intel_card([intel()], session=SESSION)
        assert "±6.2%" in card.body_md
        assert "跨财报 ±11.4%" in card.body_md
        assert "58%" in card.body_md and "41%" in card.body_md
        assert "0.71" in card.body_md
        assert "18天后财报" in card.body_md
        assert "不构成投资建议" in card.body_md

    def test_missing_values_render_dash(self) -> None:
        card = option_intel_card(
            [intel(expected_move_pct=None, expected_move_expiry=None,
                   atm_iv=None, pc_volume_ratio=None, pc_oi_ratio=None,
                   earnings_move_pct=None, earnings_move_expiry=None,
                   earnings_date=None)],
            session=SESSION,
        )
        assert "预期波动: -" in card.body_md

    def test_iv_warn_flag(self) -> None:
        card = option_intel_card(
            [intel(atm_iv=0.9, realized_vol_20d=0.4)], session=SESSION
        )
        assert "IV偏高" in card.body_md

    def test_no_data_note(self) -> None:
        card = option_intel_card(
            [intel("DRAM", data_note="无可用期权数据")], session=SESSION
        )
        assert "无可用期权数据" in card.body_md

    def test_renders_weekly_walls_gamma_opex_and_risk_references(self) -> None:
        card = option_intel_card(
            [intel(tactical=tactical())],
            session=SESSION,
        )

        assert "周线 RSI" in card.body_md
        assert "布林下轨" in card.body_md
        assert "Put墙" in card.body_md
        assert "Call墙" in card.body_md
        assert "Max Pain" in card.body_md
        assert "Gamma集中度" in card.body_md
        assert "做市商净Gamma方向不可由公开OI判定" in card.body_md
        assert "月度OPEX" in card.body_md
        assert "战术反弹" in card.body_md
        assert "失效参考" in card.body_md


def test_option_flow_card_marks_held_underlyings(tmp_path: Path) -> None:
    from quant_signal.notifier.cards import option_flow_card
    from quant_signal.options_flow import (
        OptionContractVolume,
        OptionFlowSnapshot,
        scan_slot,
    )

    at = NOW
    rows = tuple(
        OptionContractVolume(
            contract_symbol=f"{root}260717{marker}00200000",
            underlying=root,
            side=side,  # type: ignore[arg-type]
            expiration=date(2026, 7, 17),
            strike=Decimal("200"),
            volume=10_000,
            rank=1,
            venues=("cone",),
            captured_at=at,
        )
        for root, side, marker in (("MU", "call", "C"), ("SPY", "put", "P"))
    )
    snapshot = OptionFlowSnapshot(
        slot=scan_slot(at), captured_at=at, provider="cboe-four-venues",
        venue_coverage=1.0, rows=rows,
    )
    card = option_flow_card(
        snapshot, (), "baseline", held_underlyings=frozenset({"MU"})
    )
    assert "📌 MU" in card.body_md
    assert "📌 SPY" not in card.body_md
