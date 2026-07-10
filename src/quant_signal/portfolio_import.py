from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
from typing import Any
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_signal.index_universe import to_canonical_symbol

if TYPE_CHECKING:
    from quant_signal.ledger import SignalLedger

_CENT = Decimal("0.01")
_ACCOUNT_TOLERANCE = Decimal("1.00")
_WEIGHT_TOLERANCE_PCT = Decimal("0.50")


class ImportStatus(str, Enum):
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


class ExtractedAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equity: Decimal
    market_value: Decimal
    cash: Decimal
    buying_power: Decimal
    frozen_cash: Decimal = Decimal("0")
    processing_cash: Decimal = Decimal("0")
    total_unrealized_pnl: Decimal | None = None
    day_pnl: Decimal | None = None
    currency: str = "USD"
    reported_position_count: int = Field(ge=0)
    observed_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("currency is required")
        return value


class ExtractedPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    qty: Decimal | None = None
    avg_entry_price: Decimal | None = None
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    estimated_market_value: Decimal | None = None
    pnl: Decimal | None = None
    pnl_pct: Decimal | None = None
    weight_pct: Decimal | None = None
    precision: str = "ESTIMATED"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = to_canonical_symbol(value.strip().upper())
        if not symbol:
            raise ValueError("symbol is required")
        return symbol


class PortfolioExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: ExtractedAccount
    positions: tuple[ExtractedPosition, ...]


class ValidatedPortfolioImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_id: str
    image_sha256: str
    source: str = "codex_cli"
    model: str = "codex"
    uploaded_at: datetime
    observed_at: datetime
    status: ImportStatus
    account_valid: bool
    positions_complete: bool
    validation_errors: tuple[str, ...]
    capital_limit: Decimal
    max_financing_ratio: Decimal
    extraction: PortfolioExtraction
    positions: tuple[ExtractedPosition, ...]


def _non_negative_account(account: ExtractedAccount) -> bool:
    return all(
        value >= 0
        for value in (
            account.equity,
            account.market_value,
            account.cash,
            account.buying_power,
            account.frozen_cash,
            account.processing_cash,
        )
    )


def validate_extraction(
    extraction: PortfolioExtraction,
    *,
    image_sha256: str,
    uploaded_at: datetime,
    capital_limit: Decimal,
    max_financing_ratio: Decimal,
    require_account_reconciliation: bool = True,
) -> ValidatedPortfolioImport:
    errors: list[str] = []
    account = extraction.account
    symbols = [position.symbol for position in extraction.positions]
    if len(symbols) != len(set(symbols)):
        errors.append("DUPLICATE_SYMBOL")

    account_valid = _non_negative_account(account) and account.equity > 0
    if not account_valid:
        errors.append("INVALID_ACCOUNT_VALUES")
    if require_account_reconciliation:
        reconciled = account.cash + account.market_value + account.processing_cash
        if abs(account.equity - reconciled) > _ACCOUNT_TOLERANCE:
            account_valid = False
            errors.append("ACCOUNT_RECONCILIATION_FAILED")

    if account.reported_position_count != len(extraction.positions):
        errors.append("POSITION_COUNT_MISMATCH")

    positions: list[ExtractedPosition] = []
    details_complete = True
    for row in extraction.positions:
        exact = all(
            value is not None
            for value in (row.qty, row.avg_entry_price, row.current_price, row.market_value)
        )
        details_complete = details_complete and exact
        estimate = row.estimated_market_value
        if row.market_value is None and estimate is None and row.weight_pct is not None:
            estimate = (account.equity * row.weight_pct / Decimal("100")).quantize(_CENT)
        positions.append(
            row.model_copy(
                update={
                    "estimated_market_value": estimate,
                    "precision": "EXACT" if exact else "ESTIMATED",
                }
            )
        )
    if not details_complete and extraction.positions:
        errors.append("POSITION_DETAILS_INCOMPLETE")

    if require_account_reconciliation and extraction.positions:
        weights = [row.weight_pct for row in extraction.positions]
        if all(weight is not None for weight in weights) and account.equity > 0:
            expected = account.market_value / account.equity * Decimal("100")
            actual = sum((weight or Decimal("0")) for weight in weights)
            if abs(expected - actual) > _WEIGHT_TOLERANCE_PCT:
                errors.append("POSITION_WEIGHT_RECONCILIATION_FAILED")

    positions_complete = (
        account.reported_position_count == len(extraction.positions)
        and details_complete
        and "DUPLICATE_SYMBOL" not in errors
    )
    if not account_valid:
        status = ImportStatus.REJECTED
    elif errors:
        status = ImportStatus.PARTIAL
    else:
        status = ImportStatus.VALIDATED
    observed_at = account.observed_at or uploaded_at
    return ValidatedPortfolioImport(
        import_id=image_sha256,
        image_sha256=image_sha256,
        uploaded_at=uploaded_at,
        observed_at=observed_at,
        status=status,
        account_valid=account_valid,
        positions_complete=positions_complete,
        validation_errors=tuple(dict.fromkeys(errors)),
        capital_limit=capital_limit,
        max_financing_ratio=max_financing_ratio,
        extraction=extraction,
        positions=tuple(positions),
    )


def image_digest(images: Sequence[Path]) -> str:
    digest = sha256()
    for image in images:
        digest.update(image.resolve().as_posix().encode("utf-8"))
        digest.update(image.read_bytes())
    return digest.hexdigest()


def apply_validated_import(
    ledger: "SignalLedger",
    record: ValidatedPortfolioImport,
    *,
    now: datetime,
) -> bool:
    applied = ledger.save_portfolio_import(record)
    if applied and record.account_valid:
        ledger.invalidate_active_plans("ACCOUNT_CHANGED", now=now)
    return applied


def _default_run(
    args: list[str], *, input: str, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _codex_executable() -> str:
    if os.name != "nt":
        return "codex"
    return shutil.which("codex") or "codex"


class CodexPortfolioExtractor:
    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = _default_run,
        timeout_seconds: float = 180,
    ) -> None:
        self._run = run
        self._timeout_seconds = timeout_seconds

    def extract(self, images: Sequence[Path]) -> PortfolioExtraction:
        if not images:
            raise ValueError("at least one image is required")
        resolved = [image.resolve() for image in images]
        missing = [str(image) for image in resolved if not image.is_file()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        prompt = (
            "只读取所附券商持仓截图并输出符合 schema 的 JSON。"
            "禁止读取文件、运行命令或联网；禁止推测截图未展示的标的、数量、成本、现价或日期。"
            "缺失字段必须为 null；reported_position_count 使用页面显示的持仓数量。"
            "weight_pct 和 pnl_pct 使用百分数数值，例如 14.80 而不是 0.148。"
        )
        with tempfile.TemporaryDirectory(prefix="quant-signal-portfolio-") as temp:
            temp_dir = Path(temp)
            schema_path = temp_dir / "schema.json"
            output_path = temp_dir / "output.json"
            schema_path.write_text(
                json.dumps(PortfolioExtraction.model_json_schema(), ensure_ascii=False),
                encoding="utf-8",
            )
            args = [_codex_executable(), "exec"]
            for image in resolved:
                args.extend(["--image", str(image)])
            args.extend(
                [
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--ephemeral",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "-",
                ]
            )
            try:
                self._run(
                    args,
                    input=prompt,
                    cwd=temp_dir,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("portfolio screenshot extraction timed out") from error
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or "").strip()
                raise RuntimeError(
                    f"portfolio screenshot extraction failed: {detail or error.returncode}"
                ) from error
            try:
                payload: Any = json.loads(output_path.read_text(encoding="utf-8"))
                return PortfolioExtraction.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValueError) as error:
                raise RuntimeError("portfolio screenshot extraction returned invalid JSON") from error


def _uploaded_at(images: Sequence[Path]) -> datetime:
    timestamp = max(image.stat().st_mtime for image in images)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a brokerage portfolio screenshot")
    parser.add_argument("--image", action="append", type=Path, required=True)
    parser.add_argument("--capital-limit", type=Decimal, default=Decimal("6000"))
    parser.add_argument("--max-financing-ratio", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    images: list[Path] = args.image
    extraction = CodexPortfolioExtractor().extract(images)
    result = validate_extraction(
        extraction,
        image_sha256=image_digest(images),
        uploaded_at=_uploaded_at(images),
        capital_limit=args.capital_limit,
        max_financing_ratio=args.max_financing_ratio,
    )
    applied = False
    if args.apply:
        from quant_signal.config import load_settings
        from quant_signal.ledger import SignalLedger

        settings = load_settings()
        ledger = SignalLedger(settings.db_path / "signals.db")
        applied = apply_validated_import(ledger, result, now=datetime.now(timezone.utc))
    print(
        json.dumps(
            {
                "status": result.status.value,
                "account_valid": result.account_valid,
                "positions_complete": result.positions_complete,
                "reported": result.extraction.account.reported_position_count,
                "visible": len(result.positions),
                "symbols": [position.symbol for position in result.positions],
                "validation_errors": result.validation_errors,
                "applied": applied,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
