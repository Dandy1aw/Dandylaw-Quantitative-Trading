from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.portfolio_import import (  # noqa: E402
    ExtractedAccount,
    ExtractedPosition,
    ImportStatus,
    PortfolioExtraction,
    apply_validated_import,
    image_digest,
    validate_extraction,
)

IMAGE = Path(
    r"C:\Users\syw\.codex\codex-remote-attachments"
    r"\019fd73b-fe65-7e62-8850-e645583ea206"
    r"\A374D571-75AA-477A-98DE-956061F4F20D"
    r"\1-照片-1.jpg"
)
EXPECTED_CONTENT_HASH = "2f69ee56a3c71f7e7a8bc81edfc0b1bee9c891760dec7f476184f95f5ab32636"
EXPECTED_IMPORT_DIGEST = "acdf5f94adc47fb02f7c2c6ab97577465572c4b601f569a4c07c87461fc39fc3"


def extraction(observed_at: datetime) -> PortfolioExtraction:
    rows = (
        ("AAOI", "5", "129.800", "146.650", "733.25"),
        ("GOOGL", "7", "367.627", "355.255", "2486.79"),
        ("MRVL", "1", "311.305", "213.540", "213.54"),
        ("SKHY", "7", "130.680", "136.450", "955.15"),
        ("SMH", "1", "707.520", "579.950", "579.95"),
        ("MUU", "41", "25.586", "25.780", "1056.98"),
    )
    return PortfolioExtraction(
        account=ExtractedAccount(
            equity=Decimal("6045.21"),
            market_value=Decimal("6025.66"),
            cash=Decimal("19.55"),
            buying_power=Decimal("2779.33"),
            frozen_cash=Decimal("0"),
            processing_cash=Decimal("0"),
            total_unrealized_pnl=Decimal("-179.34"),
            day_pnl=Decimal("68.00"),
            currency="USD",
            reported_position_count=6,
            observed_at=observed_at,
        ),
        positions=tuple(
            ExtractedPosition(
                symbol=symbol,
                qty=Decimal(qty),
                avg_entry_price=Decimal(cost),
                current_price=Decimal(price),
                market_value=Decimal(value),
                precision="EXACT",
            )
            for symbol, qty, cost, price, value in rows
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    uploaded_at = datetime.fromtimestamp(IMAGE.stat().st_mtime, tz=timezone.utc)
    content_hash = sha256(IMAGE.read_bytes()).hexdigest()
    digest = image_digest([IMAGE])
    if content_hash != EXPECTED_CONTENT_HASH or digest != EXPECTED_IMPORT_DIGEST:
        raise RuntimeError("uploaded screenshot hash changed")
    validated = validate_extraction(
        extraction(uploaded_at),
        image_sha256=digest,
        uploaded_at=uploaded_at,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )
    if validated.status is not ImportStatus.VALIDATED:
        raise RuntimeError(f"portfolio validation failed: {validated.validation_errors}")
    expected = {"AAOI", "GOOGL", "MRVL", "SKHY", "SMH", "MUU"}
    if {row.symbol for row in validated.positions} != expected:
        raise RuntimeError("portfolio symbols do not match screenshot")

    ledger = SignalLedger(args.db.resolve())
    applied = apply_validated_import(ledger, validated, now=datetime.now(timezone.utc))
    account = ledger.latest_observed_account()
    active = ledger.active_observed_positions(exact_only=True)
    active_symbols = {str(row["symbol"]) for row in active}
    if active_symbols != expected:
        raise RuntimeError(f"active symbols mismatch: {sorted(active_symbols)}")
    result = {
        "status": validated.status.value,
        "content_sha256": content_hash,
        "import_digest": digest,
        "account_valid": validated.account_valid,
        "positions_complete": validated.positions_complete,
        "validation_errors": list(validated.validation_errors),
        "applied": applied,
        "account": {
            "equity": account["equity"] if account else None,
            "market_value": account["market_value"] if account else None,
            "cash": account["cash"] if account else None,
            "buying_power": account["buying_power"] if account else None,
        },
        "active": [
            {
                "symbol": row["symbol"],
                "qty": row["qty"],
                "avg_entry_price": row["avg_entry_price"],
                "current_price": row["current_price"],
                "market_value": row["market_value"],
            }
            for row in active
        ],
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
