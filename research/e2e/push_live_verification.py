from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings  # noqa: E402
from quant_signal.datafeed.alpaca_options import AlpacaOptionChainSource  # noqa: E402
from quant_signal.datafeed.cboe_options import CboeOptionFlowSource  # noqa: E402
from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.notifier.cards import option_flow_card, report_card  # noqa: E402
from quant_signal.notifier.feishu import get_notifier  # noqa: E402
from quant_signal.options_flow import (  # noqa: E402
    OptionContractVolume,
    build_holding_option_flow_snapshot,
)


def main() -> None:
    workspace = Path(os.environ["QUANT_E2E_WORKSPACE"])
    result = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
    settings = load_settings()
    notifier = get_notifier(settings)
    delivery: list[dict[str, object]] = []
    for title, body in zip(result["card_titles"], result["card_bodies"]):
        sent = notifier.send(report_card(str(title), str(body)))
        delivery.append({"title": title, "sent": sent})

    now = datetime.now(timezone.utc)
    cfg = settings.option_flow
    snapshot = CboeOptionFlowSource(
        venues=tuple(cfg.venues),
        discovery_limit=cfg.discovery_limit,
        top_n=cfg.top_n,
        min_venue_coverage=cfg.min_venue_coverage,
        excluded_index_roots=frozenset(cfg.excluded_index_roots),
    ).fetch(now)
    ledger = SignalLedger(settings.db_path / "signals.db")
    held: list[str] = []
    for row in ledger.active_observed_positions():
        ticker = str(row.get("symbol", "")).strip().upper()
        try:
            quantity = Decimal(str(row.get("qty", "0")))
        except (InvalidOperation, ValueError):
            continue
        if ticker and quantity > 0:
            held.append(ticker)
    held = sorted(set(held))[: cfg.holding_max_tickers]

    chain_source = AlpacaOptionChainSource(
        settings.alpaca_key,
        settings.alpaca_secret,
    )
    chains: dict[str, tuple[OptionContractVolume, ...] | None] = {}
    for ticker in held:
        try:
            chain = chain_source.fetch_chain(
                ticker,
                session=snapshot.session_date,
                max_expiry_days=cfg.holding_max_expiry_days,
                include_open_interest=False,
            )
            chains[ticker] = tuple(
                OptionContractVolume(
                    contract_symbol=contract.contract_symbol,
                    underlying=contract.underlying,
                    side=contract.side,
                    expiration=contract.expiration,
                    strike=contract.strike,
                    volume=contract.day_volume,
                    rank=rank,
                    venues=("alpaca",),
                    captured_at=now,
                )
                for rank, contract in enumerate(chain.contracts, start=1)
            )
        except Exception:
            chains[ticker] = None
    holding_snapshot = build_holding_option_flow_snapshot(
        chains,
        at=now,
        previous=None,
    )
    option_card = option_flow_card(
        snapshot,
        (),
        "query",
        enrichment_status="off",
        held_underlyings=frozenset(held),
        etf_underlyings=frozenset(cfg.etf_roots),
        holding_snapshot=holding_snapshot,
    )
    delivery.append(
        {
            "title": option_card.title,
            "sent": notifier.send(option_card),
            "venue_coverage": snapshot.venue_coverage,
            "held_underlyings": held,
        }
    )
    print(json.dumps(delivery, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
