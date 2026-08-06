from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from quant_signal.external_benchmark import (
    load_worth_buy_stocks_artifact,
    main,
)


def semantic_hash(payload: dict[str, object]) -> str:
    semantic = copy.deepcopy(payload)
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


def artifact() -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_version": "1.4",
        "contract_version": "1.4",
        "model_version": "score-v1",
        "semantic_hash": "",
        "generated_at": "2026-07-22T10:00:00Z",
        "config": {
            "symbol": "AAPL",
            "start": "2022-01-03",
            "end": "2026-07-21",
            "data_source": "cache-only",
            "feed": "iex",
            "adjustment": "split",
        },
        "provenance": {
            "db_path": "C:/private/market-data.sqlite3",
            "data_hash": "abc123",
        },
        "bars": [{"t": "2026-07-21", "c": 210.0}],
        "timeline": [{"date": "2026-07-21", "score": 80}],
        "warnings": ["survivorship bias remains"],
        "validation": {"status": "supports"},
    }
    payload["semantic_hash"] = semantic_hash(payload)
    return payload


def write_artifact(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    return path


def test_loads_verified_artifact_as_small_summary(tmp_path: Path) -> None:
    path = write_artifact(tmp_path / "artifact.json", artifact())

    summary = load_worth_buy_stocks_artifact(path)

    assert summary.source == "worth-buy-stocks"
    assert summary.symbol == "AAPL"
    assert summary.validation_status == "supports"
    assert summary.warning_count == 1
    assert "bars" not in summary.to_dict()
    assert "timeline" not in summary.to_dict()
    assert "db_path" not in json.dumps(summary.to_dict())


def test_rejects_tampered_artifact(tmp_path: Path) -> None:
    payload = artifact()
    payload["model_version"] = "tampered"
    path = write_artifact(tmp_path / "tampered.json", payload)

    with pytest.raises(ValueError, match="semantic_hash"):
        load_worth_buy_stocks_artifact(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.pop("config"), "missing fields"),
        (
            lambda item: item.__setitem__("validation", {"status": "maybe"}),
            "validation status",
        ),
        (
            lambda item: item["config"].__setitem__("symbol", "AAPL,MSFT"),  # type: ignore[union-attr]
            "single ticker",
        ),
    ],
)
def test_rejects_invalid_contract(
    tmp_path: Path, mutation: object, message: str
) -> None:
    payload = artifact()
    mutation(payload)  # type: ignore[operator]
    payload["semantic_hash"] = semantic_hash(payload)
    path = write_artifact(tmp_path / "invalid.json", payload)

    with pytest.raises(ValueError, match=message):
        load_worth_buy_stocks_artifact(path)


def test_generated_time_and_private_db_path_are_not_semantic(tmp_path: Path) -> None:
    payload = artifact()
    original_hash = payload["semantic_hash"]
    payload["generated_at"] = "2026-07-22T11:00:00Z"
    provenance = payload["provenance"]
    assert isinstance(provenance, dict)
    provenance["db_path"] = "D:/another/private.sqlite3"
    path = write_artifact(tmp_path / "moved.json", payload)

    summary = load_worth_buy_stocks_artifact(path)

    assert summary.semantic_hash == original_hash


def test_cli_prints_only_normalized_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_artifact(tmp_path / "artifact.json", artifact())

    main([str(path)])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert result["source"] == "worth-buy-stocks"
    assert result["symbol"] == "AAPL"
    assert "bars" not in result
