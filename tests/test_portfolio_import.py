from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess

import pytest

from quant_signal.ledger import SignalLedger
from quant_signal.portfolio_import import (
    CodexPortfolioExtractor,
    ExtractedAccount,
    ExtractedPosition,
    ImportStatus,
    PortfolioExtraction,
    apply_validated_import,
    validate_extraction,
)
from quant_signal.execution import PlanCandidate, PlanState, build_plan
from quant_signal.account import AccountSnapshot
from quant_signal.config import ExecutionPlanSettings

NOW = datetime(2026, 7, 11, 1, 55, tzinfo=timezone(timedelta(hours=8)))


def screenshot_extraction(
    *,
    reported_count: int = 6,
    positions: tuple[ExtractedPosition, ...] | None = None,
) -> PortfolioExtraction:
    visible = positions or (
        ExtractedPosition(symbol="DRAM", pnl="-511.13", pnl_pct="-36.54", weight_pct="14.80"),
        ExtractedPosition(symbol="MU", pnl="166.82", pnl_pct="20.23", weight_pct="16.53"),
        ExtractedPosition(symbol="RAM", pnl="45.13", pnl_pct="14.16", weight_pct="6.07"),
        ExtractedPosition(symbol="SMH", pnl="11.52", pnl_pct="0.95", weight_pct="20.46"),
        ExtractedPosition(symbol="SNXX", pnl="158.73", pnl_pct="25.77", weight_pct="12.92"),
    )
    return PortfolioExtraction(
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
            reported_position_count=reported_count,
            observed_at=NOW,
        ),
        positions=visible,
    )


def test_current_screenshot_validates_account_but_marks_positions_partial() -> None:
    result = validate_extraction(
        screenshot_extraction(),
        image_sha256="a" * 64,
        uploaded_at=NOW,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )

    assert result.status is ImportStatus.PARTIAL
    assert result.account_valid is True
    assert result.positions_complete is False
    assert set(result.validation_errors) == {
        "POSITION_COUNT_MISMATCH",
        "POSITION_DETAILS_INCOMPLETE",
    }
    assert {row.symbol for row in result.positions} == {"DRAM", "MU", "RAM", "SMH", "SNXX"}
    assert "AAPL" not in {row.symbol for row in result.positions}
    assert result.positions[0].estimated_market_value == Decimal("887.34")


def test_invalid_account_reconciliation_is_rejected() -> None:
    extraction = screenshot_extraction()
    bad = extraction.model_copy(
        update={"account": extraction.account.model_copy(update={"cash": Decimal("900")})}
    )

    result = validate_extraction(
        bad,
        image_sha256="b" * 64,
        uploaded_at=NOW,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )

    assert result.status is ImportStatus.REJECTED
    assert result.account_valid is False
    assert "ACCOUNT_RECONCILIATION_FAILED" in result.validation_errors


def test_duplicate_image_is_idempotent_and_partial_does_not_replace_complete_positions(
    tmp_path: Path,
) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    exact_positions = (
        ExtractedPosition(
            symbol="MU",
            qty="1",
            avg_entry_price="824.24",
            current_price="991.06",
            market_value="991.06",
            weight_pct="16.53",
            pnl="166.82",
            pnl_pct="20.23",
        ),
    )
    exact = validate_extraction(
        screenshot_extraction(reported_count=1, positions=exact_positions),
        image_sha256="c" * 64,
        uploaded_at=NOW - timedelta(hours=1),
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
        require_account_reconciliation=False,
    )
    partial = validate_extraction(
        screenshot_extraction(),
        image_sha256="d" * 64,
        uploaded_at=NOW,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )

    assert exact.status is ImportStatus.VALIDATED
    assert ledger.save_portfolio_import(exact) is True
    assert ledger.save_portfolio_import(exact) is False
    assert ledger.save_portfolio_import(partial) is True

    account = ledger.latest_observed_account()
    assert account is not None and account["equity"] == "5995.52"
    exact_active = ledger.active_observed_positions(exact_only=True)
    assert [row["symbol"] for row in exact_active] == ["MU"]
    risk_rows = ledger.active_observed_positions(exact_only=False)
    assert {row["symbol"] for row in risk_rows} == {"DRAM", "MU", "RAM", "SMH", "SNXX"}


def test_codex_extractor_uses_image_schema_ephemeral_and_output_file(tmp_path: Path) -> None:
    image = tmp_path / "holding.jpg"
    image.write_bytes(b"image")
    calls: list[tuple[list[str], str, Path, float]] = []

    def fake_run(
        args: list[str], *, input: str, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, input, cwd, timeout))
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(screenshot_extraction().model_dump(mode="json")),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    extraction = CodexPortfolioExtractor(run=fake_run, timeout_seconds=180).extract([image])

    args, prompt, cwd, timeout = calls[0]
    assert args[:2] == ["codex", "exec"]
    assert "--image" in args and str(image.resolve()) in args
    assert "--output-schema" in args and "--output-last-message" in args
    assert "--ephemeral" in args and "--ignore-user-config" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "禁止推测" in prompt
    assert cwd != image.parent
    assert timeout == 180
    assert extraction.account.equity == Decimal("5995.52")


def test_codex_extractor_timeout_is_safe(tmp_path: Path) -> None:
    image = tmp_path / "holding.jpg"
    image.write_bytes(b"image")

    def timeout_run(
        args: list[str], *, input: str, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        CodexPortfolioExtractor(run=timeout_run, timeout_seconds=1).extract([image])


def test_applying_new_account_invalidates_old_paper_plans(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    old_account = AccountSnapshot(
        account_id="paper-1",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("400000"),
        currency="USD",
        retrieved_at=NOW,
    )
    candidate = PlanCandidate(
        ticker="AAPL",
        plan_date=NOW.date(),
        entry_low=307.26,
        entry_high=316.22,
        stop_loss=290.49,
        take_profit=341.68,
        target_weight=None,
        score=0.9,
        source_strategies=("index_scan",),
        memberships=("sp500",),
        quote_at=NOW,
    )
    ledger.upsert_execution_plan(
        build_plan(candidate, old_account, (), (), ExecutionPlanSettings(), NOW)
    )
    result = validate_extraction(
        screenshot_extraction(),
        image_sha256="f" * 64,
        uploaded_at=NOW,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )

    assert apply_validated_import(ledger, result, now=NOW) is True
    assert ledger.active_execution_plans() == []
