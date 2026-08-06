"""Read-only interoperability with worth-buy-stocks backtest artifacts."""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import hmac
import json
from pathlib import Path
from typing import Mapping, Sequence

_VALIDATION_STATUSES = {
    "supports",
    "inconclusive",
    "contradicts",
    "invalid_run",
}
_REQUIRED_FIELDS = {
    "artifact_version",
    "contract_version",
    "model_version",
    "semantic_hash",
    "generated_at",
    "config",
    "provenance",
    "warnings",
    "validation",
}


@dataclass(frozen=True)
class ExternalBenchmarkSummary:
    source: str
    artifact_version: str
    contract_version: str
    model_version: str
    semantic_hash: str
    generated_at: str
    symbol: str
    start: str
    end: str
    data_source: str
    feed: str
    adjustment: str
    validation_status: str
    warning_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def worth_buy_semantic_hash(artifact: Mapping[str, object]) -> str:
    """Reproduce the public artifact identity contract for interoperability."""
    semantic = copy.deepcopy(dict(artifact))
    semantic.pop("semantic_hash", None)
    semantic.pop("generated_at", None)
    provenance = semantic.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("db_path", None)
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"artifact {field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"artifact {field} must be a non-empty string")
    return value.strip()


def load_worth_buy_stocks_artifact(path: Path) -> ExternalBenchmarkSummary:
    payload = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=_reject_constant
    )
    if not isinstance(payload, dict):
        raise ValueError("artifact must be a JSON object")
    missing = sorted(_REQUIRED_FIELDS - set(payload))
    if missing:
        raise ValueError(f"artifact missing fields: {', '.join(missing)}")
    config = _mapping(payload["config"], "config")
    _mapping(payload["provenance"], "provenance")
    validation = _mapping(payload["validation"], "validation")
    warnings = payload["warnings"]
    if not isinstance(warnings, list):
        raise ValueError("artifact warnings must be an array")

    expected_hash = _text(payload["semantic_hash"], "semantic_hash")
    observed_hash = worth_buy_semantic_hash(payload)
    if not hmac.compare_digest(expected_hash, observed_hash):
        raise ValueError("artifact semantic_hash mismatch")

    status = _text(validation.get("status"), "validation.status")
    if status not in _VALIDATION_STATUSES:
        raise ValueError(f"artifact validation status is unsupported: {status}")
    symbol = _text(config.get("symbol"), "config.symbol").upper()
    if "," in symbol or any(character.isspace() for character in symbol):
        raise ValueError("artifact config.symbol must contain a single ticker")
    start = _text(config.get("start"), "config.start")
    end = _text(config.get("end"), "config.end")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as error:
        raise ValueError("artifact config start/end must use YYYY-MM-DD") from error
    if start_date > end_date:
        raise ValueError("artifact config start cannot be after end")
    generated_at = _text(payload["generated_at"], "generated_at")
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("artifact generated_at must be ISO-8601") from error

    return ExternalBenchmarkSummary(
        source="worth-buy-stocks",
        artifact_version=_text(payload["artifact_version"], "artifact_version"),
        contract_version=_text(payload["contract_version"], "contract_version"),
        model_version=_text(payload["model_version"], "model_version"),
        semantic_hash=expected_hash,
        generated_at=generated_at,
        symbol=symbol,
        start=start,
        end=end,
        data_source=_text(config.get("data_source"), "config.data_source"),
        feed=_text(config.get("feed"), "config.feed"),
        adjustment=_text(config.get("adjustment"), "config.adjustment"),
        validation_status=status,
        warning_count=len(warnings),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Validate and summarize a worth-buy-stocks backtest artifact"
    )
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = load_worth_buy_stocks_artifact(args.artifact)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            summary.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False
        )
    )


if __name__ == "__main__":
    main()
