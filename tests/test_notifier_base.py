import json
from pathlib import Path

from quant_signal.notifier.base import (
    Card,
    CardKind,
    CardSection,
    ConsoleNotifier,
    card_from_dict,
    card_to_dict,
)


def test_console_send_returns_true_and_writes_jsonl(
    tmp_path: Path, capsys: object
) -> None:
    p = tmp_path / "signals.jsonl"
    n = ConsoleNotifier(jsonl_path=p)
    card = Card(kind=CardKind.SIGNAL, title="📈 NVDA buy", body_md="**现价**: 102.0")
    assert n.send(card) is True
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "NVDA" in out
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["kind"] == "signal" and rec["title"] == "📈 NVDA buy"


def test_console_send_without_jsonl(capsys: object) -> None:
    n = ConsoleNotifier()
    assert n.send(Card(kind=CardKind.ALERT, title="告警", body_md="x")) is True


def test_card_serialization_round_trip_preserves_image_key() -> None:
    card = Card(
        kind=CardKind.REPORT,
        title="Fear DCA",
        body_md="Stored recommendation",
        url="https://example.test/report",
        sections=(CardSection("Rules"), CardSection("Disclaimer")),
        image_key="img_v2_abc123",
    )

    assert card_from_dict(card_to_dict(card)) == card


def test_card_deserialization_is_backward_compatible_without_image_key() -> None:
    card = card_from_dict(
        {
            "kind": "report",
            "title": "Legacy report",
            "body_md": "No image field was stored",
            "url": None,
            "sections": ["Legacy section"],
        }
    )

    assert card.image_key is None


def test_console_jsonl_includes_image_key(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "cards.jsonl"
    notifier = ConsoleNotifier(jsonl_path=path)

    notifier.send(
        Card(
            kind=CardKind.REPORT,
            title="Fear DCA",
            body_md="Recommendation",
            image_key="img_v2_console",
        )
    )
    capsys.readouterr()  # type: ignore[attr-defined]

    assert json.loads(path.read_text(encoding="utf-8"))["image_key"] == (
        "img_v2_console"
    )
