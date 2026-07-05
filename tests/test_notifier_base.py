import json
from pathlib import Path

from quant_signal.notifier.base import Card, CardKind, ConsoleNotifier


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
