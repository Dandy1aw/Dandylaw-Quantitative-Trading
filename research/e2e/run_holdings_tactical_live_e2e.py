from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings  # noqa: E402
from quant_signal.datafeed.alpaca_options import AlpacaOptionChainSource  # noqa: E402
from quant_signal.datafeed.base import get_source  # noqa: E402
from quant_signal.datafeed.store import BarStore  # noqa: E402
from quant_signal.engine import Engine  # noqa: E402
from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.notifier.base import Card  # noqa: E402
from quant_signal.options_intel import OptionChainFetchResult  # noqa: E402
from quant_signal.pipelines.option_intel import holdings_universe, run  # noqa: E402


EXPECTED = {"GOOGL", "MRVL", "NBIS", "SKHY", "SMH"}


class RecordingNotifier:
    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


class RecordingChainSource:
    def __init__(self, delegate: AlpacaOptionChainSource) -> None:
        self._delegate = delegate
        self.calls: list[str] = []
        self.contract_counts: dict[str, int] = {}
        self.truncated: dict[str, bool] = {}

    def fetch_chain(
        self,
        underlying: str,
        *,
        session: date,
        max_expiry_days: int,
        include_open_interest: bool = True,
    ) -> OptionChainFetchResult:
        self.calls.append(underlying)
        result = self._delegate.fetch_chain(
            underlying,
            session=session,
            max_expiry_days=max_expiry_days,
            include_open_interest=include_open_interest,
        )
        self.contract_counts[underlying] = len(result.contracts)
        self.truncated[underlying] = result.truncated
        return result


def main() -> None:
    workspace = Path(os.environ["QUANT_E2E_WORKSPACE"]).resolve()
    settings = load_settings()
    notifier = RecordingNotifier()
    chain_source = RecordingChainSource(
        AlpacaOptionChainSource(settings.alpaca_key, settings.alpaca_secret)
    )
    ledger = SignalLedger(workspace / "signals.db")
    engine = Engine(
        settings,
        BarStore(workspace / "bars.duckdb"),
        get_source(settings),
        ledger,
        notifier,
        option_chain_source=chain_source,
    )
    now = datetime.now(timezone.utc)
    symbols = holdings_universe(engine)
    if not EXPECTED.issubset(symbols):
        raise RuntimeError(f"screenshot holdings missing from universe: {symbols}")

    run(engine, now)

    if len(notifier.cards) != 1:
        raise RuntimeError(f"expected one combined card, got {len(notifier.cards)}")
    card = notifier.cards[0]
    counts = Counter(chain_source.calls)
    if counts != Counter({symbol: 1 for symbol in symbols}):
        raise RuntimeError(f"option chain request counts are wrong: {counts}")
    missing_symbols = sorted(symbol for symbol in symbols if symbol not in card.body_md)
    if missing_symbols:
        raise RuntimeError(f"card omitted holdings: {missing_symbols}")
    gamma_boundary = "做市商净Gamma方向不可由公开OI判定"
    if gamma_boundary not in card.body_md:
        raise RuntimeError("card omitted the public-OI Gamma boundary")
    if "正Gamma" in card.body_md or "负Gamma" in card.body_md:
        raise RuntimeError("card inferred a signed dealer Gamma state")

    result = {
        "captured_at": now.isoformat(),
        "symbols": symbols,
        "chain_calls": dict(sorted(counts.items())),
        "contract_counts": dict(sorted(chain_source.contract_counts.items())),
        "truncated": dict(sorted(chain_source.truncated.items())),
        "card_count": len(notifier.cards),
        "card_title": card.title,
        "card_body": card.body_md,
        "all_holdings_rendered": not missing_symbols,
        "gamma_boundary_rendered": gamma_boundary in card.body_md,
        "signed_dealer_gamma_not_inferred": (
            "正Gamma" not in card.body_md and "负Gamma" not in card.body_md
        ),
    }
    (workspace / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
