from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings  # noqa: E402
from quant_signal.datafeed.alpaca_options import AlpacaOptionChainSource  # noqa: E402
from quant_signal.datafeed.base import get_source  # noqa: E402
from quant_signal.datafeed.earnings import YFinanceEarnings  # noqa: E402
from quant_signal.datafeed.fundamentals import YFinanceFundamentals  # noqa: E402
from quant_signal.datafeed.news import AlpacaNewsSource  # noqa: E402
from quant_signal.datafeed.store import BarStore  # noqa: E402
from quant_signal.engine import Engine  # noqa: E402
from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.news_store import NewsStore  # noqa: E402
from quant_signal.notifier.base import Card  # noqa: E402
from quant_signal.pipelines.us_briefing import (  # noqa: E402
    BriefingMode,
    last_completed_us_session,
    run,
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


def main() -> None:
    workspace = Path(
        os.environ.get(
            "QUANT_E2E_WORKSPACE",
            ROOT / "research" / "e2e" / "three-stage-targets-20260723",
        )
    ).resolve()
    settings = load_settings()
    notifier = RecordingNotifier()
    now = datetime.now(timezone.utc)
    report_date = last_completed_us_session(now)
    with sqlite3.connect(workspace / "signals.db") as connection:
        connection.execute(
            "DELETE FROM us_briefing_runs WHERE report_kind = ? AND as_of = ?",
            (BriefingMode.DAILY_ACTION.value, report_date.isoformat()),
        )
    ledger = SignalLedger(workspace / "signals.db")
    option_chain = AlpacaOptionChainSource(
        settings.alpaca_key,
        settings.alpaca_secret,
    )
    news_source = (
        AlpacaNewsSource(settings.alpaca_key, settings.alpaca_secret)
        if settings.alpaca_key and settings.alpaca_secret
        else None
    )
    engine = Engine(
        settings,
        BarStore(workspace / "bars.duckdb"),
        get_source(settings),
        ledger,
        notifier,
        earnings_source=YFinanceEarnings(),
        fundamentals_source=YFinanceFundamentals(),
        news_source=news_source,
        news_store=NewsStore(workspace / "news.db"),
        option_chain_source=option_chain,
    )
    delivered = run(engine, now, BriefingMode.DAILY_ACTION, deliver=True)
    candidates = ledger.candidate_lane_snapshot("DAILY_ACTION", report_date)

    target_checks: list[dict[str, object]] = []
    for row in candidates:
        targets = [float(value) for value in row.get("profit_targets", [])]
        entry = float(row["entry_high"])
        stage = int(row.get("recommended_target_stage", 0))
        target_checks.append(
            {
                "ticker": row["ticker"],
                "entry_high": entry,
                "targets": targets,
                "expected": [round(entry * factor, 4) for factor in (1.05, 1.10, 1.15)],
                "stage": stage,
                "target_price": float(row["target_price"]),
                "buying_pressure_label": row.get("buying_pressure_label"),
                "buying_pressure_score": row.get("buying_pressure_score"),
                "recent_buying_notional": row.get("recent_buying_notional"),
                "nearby_resistance": row.get("nearby_resistance"),
            }
        )

    session = now.astimezone(ZoneInfo("America/New_York")).date()
    nvda_chain = option_chain.fetch_chain(
        "NVDA",
        session=session,
        max_expiry_days=settings.option_flow.holding_max_expiry_days,
        include_open_interest=False,
    )
    call_volume = sum(
        contract.day_volume
        for contract in nvda_chain.contracts
        if contract.side == "call"
    )
    put_volume = sum(
        contract.day_volume
        for contract in nvda_chain.contracts
        if contract.side == "put"
    )

    result = {
        "captured_at": now.isoformat(),
        "report_date": report_date.isoformat(),
        "delivered_to_recording_notifier": delivered,
        "card_count": len(notifier.cards),
        "card_titles": [card.title for card in notifier.cards],
        "card_bodies": [card.body_md for card in notifier.cards],
        "company_rationale_count": sum(
            card.body_md.count("上涨逻辑：") for card in notifier.cards
        ),
        "all_live_labels_translated": all(
            raw not in "\n".join(card.body_md for card in notifier.cards)
            for raw in ("ZERO_QTY", "MAX_NEW_POSITIONS", "Communication Equipment", "Advertising Agencies")
        ),
        "candidate_count": len(candidates),
        "target_checks": target_checks,
        "all_three_stage_targets_exact": all(
            row["targets"] == row["expected"] and 1 <= int(row["stage"]) <= 3
            for row in target_checks
        ),
        "nvda_live_option_chain": {
            "session": session.isoformat(),
            "contract_count": len(nvda_chain.contracts),
            "call_volume": call_volume,
            "put_volume": put_volume,
            "call_put_ratio": call_volume / put_volume if put_volume else None,
            "truncated": nvda_chain.truncated,
        },
    }
    (workspace / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
