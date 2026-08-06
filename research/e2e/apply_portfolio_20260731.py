from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.config import load_settings  # noqa: E402
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
    r"\019f7b14-0938-73a3-a707-c9a20a4971e8"
    r"\56697BDD-3FE0-4E39-B69C-6371AF05E040"
    r"\1-照片-1.jpg"
)


def main() -> None:
    uploaded_at = datetime.fromtimestamp(IMAGE.stat().st_mtime, tz=timezone.utc)
    extraction = PortfolioExtraction(
        account=ExtractedAccount(
            equity=Decimal("5747.18"),
            market_value=Decimal("4709.07"),
            cash=Decimal("1037.57"),
            buying_power=Decimal("2961.97"),
            frozen_cash=Decimal("0"),
            processing_cash=Decimal("0"),
            total_unrealized_pnl=Decimal("-574.33"),
            day_pnl=Decimal("672.38"),
            currency="USD",
            reported_position_count=5,
            observed_at=uploaded_at,
        ),
        positions=(
            ExtractedPosition(
                symbol="GOOGL",
                qty=Decimal("2"),
                avg_entry_price=Decimal("391.943"),
                current_price=Decimal("333.430"),
                market_value=Decimal("666.86"),
                pnl=Decimal("-117.03"),
                precision="EXACT",
            ),
            ExtractedPosition(
                symbol="MRVL",
                qty=Decimal("6"),
                avg_entry_price=Decimal("207.875"),
                current_price=Decimal("182.404"),
                market_value=Decimal("1094.42"),
                pnl=Decimal("-152.83"),
                precision="EXACT",
            ),
            ExtractedPosition(
                symbol="NBIS",
                qty=Decimal("5"),
                avg_entry_price=Decimal("213.770"),
                current_price=Decimal("191.154"),
                market_value=Decimal("955.77"),
                pnl=Decimal("-113.08"),
                precision="EXACT",
            ),
            ExtractedPosition(
                symbol="SKHY",
                qty=Decimal("10"),
                avg_entry_price=Decimal("147.589"),
                current_price=Decimal("145.571"),
                market_value=Decimal("1455.71"),
                pnl=Decimal("-20.18"),
                precision="EXACT",
            ),
            ExtractedPosition(
                symbol="SMH",
                qty=Decimal("1"),
                avg_entry_price=Decimal("707.520"),
                current_price=Decimal("536.305"),
                market_value=Decimal("536.31"),
                pnl=Decimal("-171.22"),
                precision="EXACT",
            ),
        ),
    )
    settings = load_settings()
    result = validate_extraction(
        extraction,
        image_sha256=image_digest([IMAGE]),
        uploaded_at=uploaded_at,
        capital_limit=Decimal(str(settings.execution_plan.capital_limit_usd)),
        max_financing_ratio=Decimal(
            str(settings.execution_plan.max_financing_ratio)
        ),
    )
    if result.status != ImportStatus.VALIDATED:
        raise RuntimeError(
            f"portfolio validation failed: {result.validation_errors}"
        )
    expected = {"GOOGL", "MRVL", "NBIS", "SKHY", "SMH"}
    if {position.symbol for position in result.positions} != expected:
        raise RuntimeError("portfolio symbols do not match the screenshot")
    ledger = SignalLedger(settings.db_path / "signals.db")
    applied = apply_validated_import(
        ledger,
        result,
        now=datetime.now(timezone.utc),
    )
    active = ledger.active_observed_positions(exact_only=True)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "account_valid": result.account_valid,
                "positions_complete": result.positions_complete,
                "validation_errors": result.validation_errors,
                "applied": applied,
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
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
